"""FVG, Order Block, liquidity sweep, ATR."""
from __future__ import annotations

from typing import List, Optional

import numpy as np
import pandas as pd

import config as cfg
from config import (
    ATR_PERIOD,
    LIQUIDITY_TOLERANCE_PCT,
    OB_LOOKBACK,
    SWEEP_LOOKBACK,
    VOL_SMA_BARS,
)
from ict.models import Swing, Sweep, Zone


# --------------------------------------------------------------------- ATR
def atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    prev_c = c.shift(1)
    tr = pd.concat([(h - l), (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


# --------------------------------------------------------------------- FVG
def detect_fvgs(df: pd.DataFrame) -> List[Zone]:
    """Three-candle imbalance. Bullish FVG = candle[i].low > candle[i-2].high.

    A zone is dropped if later price has fully traded through it (mitigated).
    """
    if len(df) < 3:
        return []

    a = atr(df).fillna(0.0).values
    highs = df["high"].values
    lows = df["low"].values
    zones: List[Zone] = []

    for i in range(2, len(df)):
        thresh = a[i] * cfg.FVG_MIN_ATR_MULT
        # bullish FVG between candle i-2 (high) and candle i (low)
        if lows[i] > highs[i - 2] and lows[i] - highs[i - 2] >= thresh:
            top, bottom = float(lows[i]), float(highs[i - 2])
            zones.append(Zone(kind="FVG", direction="long", top=top, bottom=bottom, origin_idx=i - 1))
        # bearish FVG
        if highs[i] < lows[i - 2] and lows[i - 2] - highs[i] >= thresh:
            top, bottom = float(lows[i - 2]), float(highs[i])
            zones.append(Zone(kind="FVG", direction="short", top=top, bottom=bottom, origin_idx=i - 1))

    _mark_mitigated(zones, df)
    return [z for z in zones if not z.mitigated]


# ---------------------------------------------------------------- Order Block
def detect_order_blocks(df: pd.DataFrame, swings: List[Swing]) -> List[Zone]:
    """Last opposite-color candle immediately before a displacement swing.

    Bullish OB: last bearish candle before a strong up-swing that broke
    recent structure. We take a simplified form — the last down candle
    before a significant up move is the OB.
    """
    if len(df) < 5 or not swings:
        return []

    opens = df["open"].values
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    a = atr(df).fillna(0.0).values
    zones: List[Zone] = []

    start = max(2, len(df) - OB_LOOKBACK)
    for i in range(start, len(df) - 1):
        body = closes[i] - opens[i]
        disp = a[i] * 1.2 if a[i] > 0 else 0
        # bullish OB: current is down candle, next candle pushes up strongly
        if body < 0 and (closes[i + 1] - opens[i + 1]) > disp:
            zones.append(
                Zone(
                    kind="OB",
                    direction="long",
                    top=float(max(opens[i], closes[i])),
                    bottom=float(lows[i]),
                    origin_idx=i,
                )
            )
        # bearish OB: up candle, then strong down candle
        if body > 0 and (opens[i + 1] - closes[i + 1]) > disp:
            zones.append(
                Zone(
                    kind="OB",
                    direction="short",
                    top=float(highs[i]),
                    bottom=float(min(opens[i], closes[i])),
                    origin_idx=i,
                )
            )

    _mark_mitigated(zones, df)
    return [z for z in zones if not z.mitigated]


def _mark_mitigated(zones: List[Zone], df: pd.DataFrame) -> None:
    highs = df["high"].values
    lows = df["low"].values
    for z in zones:
        for j in range(z.origin_idx + 1, len(df)):
            if z.direction == "long" and lows[j] <= z.bottom:
                z.mitigated = True
                break
            if z.direction == "short" and highs[j] >= z.top:
                z.mitigated = True
                break


# ------------------------------------------------------------- Liquidity sweep
def detect_sweep(df: pd.DataFrame, lookback: int = SWEEP_LOOKBACK) -> Optional[Sweep]:
    """Did the most recent bar wick through a prior extreme and close back?

    Long sweep (bearish liquidity taken) → expect reversal up.
    Short sweep (bullish liquidity taken) → expect reversal down.

    Quality gates (Bouchaud & Bonart 2018; Karpoff 1987):
      * the rejecting wick must cover at least SWEEP_WICK_RATIO of the
        candle range — true stop-runs leave a long tail, soft drifts do not
      * the candle's volume must exceed VOL_MULT_SWEEP × SMA(VOL_SMA_BARS).
    """
    if len(df) < max(lookback, VOL_SMA_BARS) + 2:
        return None

    i = len(df) - 1
    tail = df.iloc[max(0, i - lookback) : i]
    last_low = float(df["low"].iloc[i])
    last_close = float(df["close"].iloc[i])
    last_high = float(df["high"].iloc[i])
    last_open = float(df["open"].iloc[i])
    last_vol = float(df["volume"].iloc[i])
    rng = max(last_high - last_low, 1e-12)

    vol_sma = float(df["volume"].iloc[max(0, i - VOL_SMA_BARS) : i].mean() or 0)
    vol_ok = vol_sma > 0 and last_vol >= vol_sma * cfg.VOL_MULT_SWEEP

    prior_low = float(tail["low"].min())
    prior_high = float(tail["high"].max())
    tol_low = prior_low * (1 - LIQUIDITY_TOLERANCE_PCT)
    tol_high = prior_high * (1 + LIQUIDITY_TOLERANCE_PCT)

    body_top = max(last_open, last_close)
    body_bot = min(last_open, last_close)
    lower_wick = body_bot - last_low
    upper_wick = last_high - body_top

    if last_low < tol_low and last_close > prior_low:
        if (lower_wick / rng) >= cfg.SWEEP_WICK_RATIO and vol_ok:
            return Sweep(direction="long", idx=i, swept_level=prior_low)
    if last_high > tol_high and last_close < prior_high:
        if (upper_wick / rng) >= cfg.SWEEP_WICK_RATIO and vol_ok:
            return Sweep(direction="short", idx=i, swept_level=prior_high)
    return None


def volume_confirms(df: pd.DataFrame, idx: int, mult: float, bars: int = VOL_SMA_BARS) -> bool:
    """True iff the candle at idx has volume >= mult * SMA(bars)."""
    if idx <= 0 or idx >= len(df):
        return False
    start = max(0, idx - bars)
    sma = float(df["volume"].iloc[start:idx].mean() or 0)
    if sma <= 0:
        return False
    return float(df["volume"].iloc[idx]) >= sma * mult


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()
