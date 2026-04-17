"""HTF -> MTF -> LTF top-down ICT pipeline.

Each timeframe's analysis is strictly conditioned on the previous one:

 1. HTF: establish bias (HH/HL vs LH/LL), confirm a liquidity sweep exists
    in the direction of bias, collect unmitigated FVG/OB zones aligned
    with the bias.
 2. MTF: require current price inside an HTF zone; confirm MTF structure
    (CHoCH or BOS) agreeing with HTF bias; keep MTF zones nested inside
    the HTF zone for refinement.
 3. LTF: final trigger — LTF sweep + LTF CHoCH in HTF direction + LTF
    FVG/OB at current price. Signal carries entry / SL / TP; sizing is
    handed to the risk module.
"""
from __future__ import annotations

from typing import List, Optional

import pandas as pd

from config import MIN_RR
from ict.models import Bias, Signal, TFAnalysis, Zone
from ict.patterns import detect_fvgs, detect_order_blocks, detect_sweep
from ict.structure import detect_structure, detect_swings
from utils.logger import get_logger

log = get_logger("ict")


def analyze(df: pd.DataFrame, tf: str) -> TFAnalysis:
    swings = detect_swings(df)
    events, bias = detect_structure(df, swings)
    fvgs = detect_fvgs(df)
    obs = detect_order_blocks(df, swings)
    sweep = detect_sweep(df)
    return TFAnalysis(
        tf=tf,
        bias=bias,
        swings=swings,
        events=events,
        zones=fvgs + obs,
        sweep=sweep,
        last_close=float(df["close"].iloc[-1]),
    )


def _zones_in_direction(a: TFAnalysis, direction: str) -> List[Zone]:
    return [z for z in a.zones if z.direction == direction and not z.mitigated]


def _zone_containing(zones: List[Zone], price: float) -> Optional[Zone]:
    for z in zones:
        if z.contains(price):
            return z
    return None


def _zones_inside(zones: List[Zone], outer: Zone) -> List[Zone]:
    return [z for z in zones if z.bottom >= outer.bottom and z.top <= outer.top]


def _last_event(a: TFAnalysis, direction: str) -> Optional[str]:
    for e in reversed(a.events):
        if e.direction == direction:
            return e.kind
    return None


def _next_liquidity_target(a: TFAnalysis, direction: str, price: float) -> Optional[float]:
    """Nearest prior opposite-side swing serves as a liquidity target (TP)."""
    if direction == "long":
        highs = [s.price for s in a.swings if s.is_high and s.price > price]
        return min(highs) if highs else None
    lows = [s.price for s in a.swings if not s.is_high and s.price < price]
    return max(lows) if lows else None


def top_down(
    symbol: str,
    htf_df: pd.DataFrame,
    mtf_df: pd.DataFrame,
    ltf_df: pd.DataFrame,
) -> Optional[Signal]:
    # -------- HTF -----------------------------------------------------------
    htf = analyze(htf_df, "HTF")
    if htf.bias is Bias.NONE:
        return None

    direction = "long" if htf.bias is Bias.BULL else "short"
    if htf.sweep is None or htf.sweep.direction != direction:
        return None

    htf_zones = _zones_in_direction(htf, direction)
    if not htf_zones:
        return None

    price = float(ltf_df["close"].iloc[-1])
    htf_zone = _zone_containing(htf_zones, price)
    if htf_zone is None:
        return None

    # -------- MTF -----------------------------------------------------------
    mtf = analyze(mtf_df, "MTF")
    if mtf.bias is not htf.bias:
        return None
    if _last_event(mtf, direction) not in ("BOS", "CHOCH"):
        return None

    mtf_zones_dir = _zones_in_direction(mtf, direction)
    mtf_zones_nested = _zones_inside(mtf_zones_dir, htf_zone) or mtf_zones_dir
    mtf_zone = _zone_containing(mtf_zones_nested, price)
    if mtf_zone is None:
        return None

    # -------- LTF -----------------------------------------------------------
    ltf = analyze(ltf_df, "LTF")
    if ltf.sweep is None or ltf.sweep.direction != direction:
        return None
    if _last_event(ltf, direction) != "CHOCH":
        return None

    ltf_zone = _zone_containing(_zones_in_direction(ltf, direction), price)
    if ltf_zone is None:
        return None

    # -------- Entry / SL / TP ---------------------------------------------
    if direction == "long":
        entry = ltf_zone.top
        sl = min(ltf_zone.bottom, ltf.sweep.swept_level) * 0.999
        tp = _next_liquidity_target(htf, "long", entry) or (entry + (entry - sl) * MIN_RR)
    else:
        entry = ltf_zone.bottom
        sl = max(ltf_zone.top, ltf.sweep.swept_level) * 1.001
        tp = _next_liquidity_target(htf, "short", entry) or (entry - (sl - entry) * MIN_RR)

    reason = (
        f"HTF {htf.bias.value} sweep@{htf.sweep.swept_level:.4f} inside {htf_zone.kind} | "
        f"MTF {_last_event(mtf, direction)} inside {mtf_zone.kind} | "
        f"LTF sweep@{ltf.sweep.swept_level:.4f} + CHoCH → {ltf_zone.kind}"
    )

    sig = Signal(
        symbol=symbol,
        direction=direction,
        entry=entry,
        sl=sl,
        tp=tp,
        reason=reason,
        htf=htf,
        mtf=mtf,
        ltf=ltf,
    )

    if sig.rr < MIN_RR:
        log.debug("%s rejected on RR=%.2f", symbol, sig.rr)
        return None
    return sig
