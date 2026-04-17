"""Dow-theory swing detection and market-structure events (BOS / CHoCH / BSS).

A "swing high" is a bar whose high is the strict maximum inside a
[-lookback, +lookback] window; symmetric for swing lows. Each swing is
then labeled HH / HL / LH / LL by comparison to the *prior* swing of the
same polarity.

Structure events walk the labeled swings chronologically:

 * current bias is derived from the most recent labeled swing pair
 * a BOS is logged whenever price closes beyond the last same-direction
   swing while the bias is in that same direction
 * a CHoCH is logged when price closes beyond an opposite-direction swing
   against the prior bias — this flips the bias
 * a BSS ("breaker structure shift") is a CHoCH immediately preceded by
   a liquidity sweep (handled in topdown.py where sweep data is joined)
"""
from __future__ import annotations

from typing import List

import pandas as pd

from config import STRUCTURE_LOOKBACK, SWING_LOOKBACK
from ict.models import Bias, StructureEvent, Swing


# ---------------------------------------------------------------- swings
def detect_swings(df: pd.DataFrame, lookback: int = SWING_LOOKBACK) -> List[Swing]:
    """Return swings in chronological order, each labeled HH/HL/LH/LL."""
    highs = df["high"].values
    lows = df["low"].values
    swings: List[Swing] = []

    for i in range(lookback, len(df) - lookback):
        hi_window = highs[i - lookback : i + lookback + 1]
        lo_window = lows[i - lookback : i + lookback + 1]
        if highs[i] == hi_window.max() and (hi_window == highs[i]).sum() == 1:
            swings.append(Swing(idx=i, price=float(highs[i]), kind=None, is_high=True))
        if lows[i] == lo_window.min() and (lo_window == lows[i]).sum() == 1:
            swings.append(Swing(idx=i, price=float(lows[i]), kind=None, is_high=False))

    swings.sort(key=lambda s: s.idx)
    _label_swings(swings)
    return swings


def _label_swings(swings: List[Swing]) -> None:
    last_high: Swing | None = None
    last_low: Swing | None = None
    for s in swings:
        if s.is_high:
            if last_high is None:
                s.kind = "HH"
            else:
                s.kind = "HH" if s.price > last_high.price else "LH"
            last_high = s
        else:
            if last_low is None:
                s.kind = "HL"
            else:
                s.kind = "HL" if s.price > last_low.price else "LL"
            last_low = s


# --------------------------------------------------------- structure events
def detect_structure(
    df: pd.DataFrame,
    swings: List[Swing],
    lookback: int = STRUCTURE_LOOKBACK,
) -> tuple[list[StructureEvent], Bias]:
    """Walk bars forward from the earliest swing, emitting BOS / CHoCH.

    Returns (events, current_bias).
    """
    events: List[StructureEvent] = []
    if len(swings) < 2 or len(df) == 0:
        return events, Bias.NONE

    closes = df["close"].values
    start = max(0, len(df) - lookback)

    bias = Bias.NONE
    # track the most recent unbroken swing extremes to test against
    pending_high = _last_before(swings, start, is_high=True)
    pending_low = _last_before(swings, start, is_high=False)

    for i in range(start, len(df)):
        # ingest any new swing that fully formed by bar i
        for s in swings:
            if s.idx > i:
                break
            if s.idx == i:
                if s.is_high:
                    pending_high = s
                else:
                    pending_low = s

        c = float(closes[i])

        if pending_high and c > pending_high.price:
            kind = "BOS" if bias == Bias.BULL else "CHOCH"
            events.append(
                StructureEvent(
                    kind=kind,
                    direction="long",
                    idx=i,
                    broken_price=pending_high.price,
                )
            )
            bias = Bias.BULL
            pending_high = _next_after(swings, i, is_high=True)

        if pending_low and c < pending_low.price:
            kind = "BOS" if bias == Bias.BEAR else "CHOCH"
            events.append(
                StructureEvent(
                    kind=kind,
                    direction="short",
                    idx=i,
                    broken_price=pending_low.price,
                )
            )
            bias = Bias.BEAR
            pending_low = _next_after(swings, i, is_high=False)

    return events, bias


def _last_before(swings: List[Swing], idx: int, *, is_high: bool) -> Swing | None:
    pick = None
    for s in swings:
        if s.is_high != is_high:
            continue
        if s.idx >= idx:
            break
        pick = s
    return pick


def _next_after(swings: List[Swing], idx: int, *, is_high: bool) -> Swing | None:
    for s in swings:
        if s.is_high != is_high:
            continue
        if s.idx > idx:
            return s
    return None
