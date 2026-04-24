"""Grid-search the ICT pipeline on cached historical data.

Usage:
  # 1) one-time: fetch ~30 days of HTF/MTF/LTF for a small symbol set
  python scripts/backtest.py fetch --days 30
  python scripts/backtest.py fetch --days 60 --symbols BTC,ETH,SOL,BNB,XRP,DOGE,LINK,AVAX

  # 2) replay every config in the small grid (~30 configs, ~10 min)
  python scripts/backtest.py run

  # 3) replay an explicit single config (for debugging / one-off check)
  python scripts/backtest.py run --single \
    --override MIN_RR=1.2 --override HTF_MOMENTUM_FILTER=False

  # 4) overnight grid (~hundreds of configs)
  python scripts/backtest.py run --grid large

Output: ranked table on stdout, full results json at logs/backtest_last.json.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as cfg
from backtest.data import _read_cache, _cache_path, prefetch_universe
from backtest.engine import BacktestResult, run_backtest
from backtest.grid import (
    LARGE_GRID,
    SMALL_GRID,
    expand_grid,
    format_table,
    label_for,
    pareto_front,
    rank,
)


DEFAULT_SYMBOLS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "LINK", "AVAX"]
RESULTS_FILE = Path(cfg.LOG_DIR) / "backtest_last.json"


def _to_ccxt(base: str) -> str:
    return f"{base.upper()}/USDT:USDT"


def _parse_override(s: str) -> tuple[str, object]:
    if "=" not in s:
        raise SystemExit(f"--override expects KEY=VAL, got {s!r}")
    k, v = s.split("=", 1)
    # try bool / int / float / else string
    low = v.lower()
    if low in ("true", "false"):
        return k, low == "true"
    try:
        return k, int(v)
    except ValueError:
        pass
    try:
        return k, float(v)
    except ValueError:
        pass
    return k, v


def _check_cache(symbols: List[str], days: int) -> List[str]:
    missing = []
    for sym in symbols:
        ccxt_sym = _to_ccxt(sym)
        for tf in (cfg.HTF_TIMEFRAME, cfg.MTF_TIMEFRAME, cfg.LTF_TIMEFRAME):
            if not _cache_path(ccxt_sym, tf, days).exists():
                missing.append(f"{sym}/{tf}")
    return missing


def _load_cache(symbols: List[str], days: int) -> Dict[str, Dict[str, "object"]]:
    import pandas as pd
    cache: Dict[str, Dict[str, pd.DataFrame]] = {}
    for sym in symbols:
        ccxt_sym = _to_ccxt(sym)
        cache[ccxt_sym] = {}
        for tf in (cfg.HTF_TIMEFRAME, cfg.MTF_TIMEFRAME, cfg.LTF_TIMEFRAME):
            p = _cache_path(ccxt_sym, tf, days)
            if not p.exists():
                continue
            cache[ccxt_sym][tf] = _read_cache(p)
    return cache


# ---------------------------------------------------------------- subcommands
async def cmd_fetch(args: argparse.Namespace) -> int:
    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    pairs = [_to_ccxt(s) for s in syms]
    print(f"== fetching {len(pairs)} symbols × 3 timeframes × {args.days}d")
    start = time.time()
    await prefetch_universe(
        pairs,
        [cfg.HTF_TIMEFRAME, cfg.MTF_TIMEFRAME, cfg.LTF_TIMEFRAME],
        args.days,
        use_cache=not args.refresh,
    )
    print(f"== fetched in {time.time()-start:.1f}s — cache at {Path(cfg.LOG_DIR)/'backtest_cache'}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    missing = _check_cache(syms, args.days)
    if missing:
        print(f"!! missing cache for: {missing[:6]}{'...' if len(missing)>6 else ''}")
        print(f"   run: python scripts/backtest.py fetch --days {args.days} --symbols {','.join(syms)}")
        return 2
    cache = _load_cache(syms, args.days)
    bars = sum(len(df) for by_tf in cache.values() for df in by_tf.values())
    print(f"== loaded {len(cache)} symbols, {bars} total bars")

    if args.single:
        overrides = dict(_parse_override(s) for s in args.override or [])
        configs = [overrides]
    else:
        grid = LARGE_GRID if args.grid == "large" else SMALL_GRID
        configs = expand_grid(grid)
    print(f"== evaluating {len(configs)} configurations (eval_every={args.eval_every})")

    results: List[BacktestResult] = []
    t0 = time.time()
    for i, combo in enumerate(configs, 1):
        label = label_for(combo)
        r = run_backtest(label, combo, cache, eval_every=args.eval_every)
        results.append(r)
        elapsed = time.time() - t0
        eta = (elapsed / i) * (len(configs) - i)
        print(
            f"  [{i:3d}/{len(configs)}] sigs={r.signal_count:4d} "
            f"trades={len(r.trades):4d} totR={r.total_r:+6.1f} "
            f"avgR={r.avg_r:+.2f} | eta {eta/60:5.1f}m | {label[:80]}"
        )

    print()
    print("== TOP 25 BY COMPOSITE SCORE (expectancy_R × √trades) ==")
    print(format_table(rank(results, min_trades=args.min_trades), top_n=25))
    print()
    print("== PARETO FRONT (signals ↑ AND total_R ↑) ==")
    print(format_table(pareto_front(results), top_n=25))
    print()
    print("== TOP 10 BY TOTAL_R (any trade count) ==")
    print(format_table(rank(results, min_trades=1, score="total"), top_n=10))

    RESULTS_FILE.write_text(json.dumps([r.as_dict() for r in results], indent=2))
    print(f"\n== full results saved to {RESULTS_FILE}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="download historical OHLCV into cache")
    f.add_argument("--days", type=int, default=30)
    f.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    f.add_argument("--refresh", action="store_true", help="ignore existing cache")

    r = sub.add_parser("run", help="replay grid against cached data")
    r.add_argument("--days", type=int, default=30)
    r.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    r.add_argument("--grid", choices=["small", "large"], default="small")
    r.add_argument("--single", action="store_true",
                   help="run only one config (uses --override values, ignores grid)")
    r.add_argument("--override", action="append",
                   help="KEY=VAL config override (repeatable, only with --single)")
    r.add_argument("--eval-every", type=int, default=5,
                   help="evaluate top_down() every N LTF bars (1=every bar). "
                        "Higher = much faster, lower = more signals captured.")
    r.add_argument("--min-trades", type=int, default=5,
                   help="filter ranking to configs with at least this many trades")

    args = ap.parse_args()

    if args.cmd == "fetch":
        return asyncio.run(cmd_fetch(args))
    if args.cmd == "run":
        return cmd_run(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
