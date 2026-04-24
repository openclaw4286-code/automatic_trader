"""Historical OHLCV cache for backtesting.

Gate.io's REST limits us to ~1000 candles per call, so we paginate
backwards from `until_ms` until we have at least `min_bars`.
Results are cached as CSV under logs/backtest_cache/.

CSV (not parquet) so the cache works without pyarrow / fastparquet —
30 days of 3m bars is ~14k rows × 6 cols, which loads in <100ms from
CSV. The cache exists to avoid re-fetching the same network bars
across grid runs, not to compete with a database.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Dict, List

import pandas as pd

from config import LOG_DIR
from exchange.gateio import GateioFutures
from utils.logger import get_logger

log = get_logger("backtest.data")

CACHE_DIR = Path(LOG_DIR) / "backtest_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Gate.io accepts these timeframe codes; minutes-per-bar table powers paging.
_TF_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "1d": 1440,
}


def _cache_path(symbol: str, timeframe: str, days: int) -> Path:
    safe = symbol.replace("/", "_").replace(":", "_")
    return CACHE_DIR / f"{safe}__{timeframe}__{days}d.csv"


def _read_cache(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df


async def fetch_history(
    ex: GateioFutures,
    symbol: str,
    timeframe: str,
    days: int,
    *,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Return >= ``days`` of OHLCV ending now, paginating older."""
    cache = _cache_path(symbol, timeframe, days)
    if use_cache and cache.exists():
        df = _read_cache(cache)
        # Refresh if cache is more than 6h stale relative to its last bar.
        last_ts = df["ts"].iloc[-1]
        age_h = (pd.Timestamp.now(tz="UTC") - last_ts).total_seconds() / 3600
        if age_h < 6:
            return df
        log.info("cache stale (%.1fh) — refetching %s %s", age_h, symbol, timeframe)

    minutes_per_bar = _TF_MINUTES[timeframe]
    total_bars_needed = int((days * 24 * 60) / minutes_per_bar) + 5
    chunk = 1000
    step_ms = chunk * minutes_per_bar * 60_000
    collected: Dict[int, List[float]] = {}    # ts -> row (dedupe-by-ts)
    until_ms = int(time.time() * 1000)
    max_iters = 60                            # hard cap so we never loop forever

    for _ in range(max_iters):
        if len(collected) >= total_bars_needed:
            break
        since = until_ms - step_ms
        try:
            batch = await ex._ex.fetch_ohlcv(
                symbol, timeframe=timeframe, since=since, limit=chunk
            )
        except Exception as exc:
            log.warning("fetch_ohlcv failed %s %s: %s", symbol, timeframe, exc)
            break
        if not batch:
            break
        # Only keep rows strictly older than our current cursor — otherwise
        # we'd re-collect the same newest-batch every iteration. Gate / ccxt
        # happily return a short batch (e.g. 997) on a full 1000-limit call,
        # so deciding "done" on batch size alone (the old bug) stopped us
        # after a single page. Instead we stop when a whole page yields zero
        # NEW rows, which is what "no more history" actually looks like.
        new = 0
        for row in batch:
            ts = int(row[0])
            if ts < until_ms and ts not in collected:
                collected[ts] = row
                new += 1
        if new == 0:
            break
        until_ms = min(collected)
        await asyncio.sleep(0.25)   # be nice to the API

    if not collected:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])

    rows = [collected[ts] for ts in sorted(collected)]
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df.to_csv(cache, index=False)
    span_days = (df["ts"].iloc[-1] - df["ts"].iloc[0]).total_seconds() / 86400
    log.info(
        "cached %d %s %s bars (%.1f days) → %s",
        len(df), symbol, timeframe, span_days, cache.name,
    )
    return df


async def prefetch_universe(
    symbols: List[str],
    timeframes: List[str],
    days: int,
    use_cache: bool = True,
) -> Dict[str, Dict[str, pd.DataFrame]]:
    """Fetch every (symbol, timeframe) pair concurrently. Returns
    out[symbol][timeframe] = DataFrame."""
    ex = GateioFutures()
    out: Dict[str, Dict[str, pd.DataFrame]] = {}
    try:
        await ex.load()
        sem = asyncio.Semaphore(4)

        async def _one(sym: str, tf: str) -> None:
            async with sem:
                df = await fetch_history(ex, sym, tf, days, use_cache=use_cache)
                out.setdefault(sym, {})[tf] = df

        tasks = [_one(s, tf) for s in symbols for tf in timeframes]
        await asyncio.gather(*tasks)
    finally:
        await ex.close()
    return out
