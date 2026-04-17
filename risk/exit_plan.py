"""Derived price levels for partial-TP, break-even, and trailing stops."""
from __future__ import annotations

from config import TP1_RR, TRAIL_ACTIVATION_RR, TRAIL_DISTANCE_R


def risk_unit(entry: float, sl: float) -> float:
    return abs(entry - sl)


def tp1_price(entry: float, sl: float, direction: str) -> float:
    r = risk_unit(entry, sl)
    return entry + r * TP1_RR if direction == "long" else entry - r * TP1_RR


def trail_activation_price(entry: float, sl: float, direction: str) -> float:
    r = risk_unit(entry, sl)
    return entry + r * TRAIL_ACTIVATION_RR if direction == "long" else entry - r * TRAIL_ACTIVATION_RR


def trailing_sl(best_price: float, entry: float, initial_sl: float, direction: str) -> float:
    """Return a proposed new SL based on the best price reached so far."""
    r = risk_unit(entry, initial_sl)
    offset = r * TRAIL_DISTANCE_R
    return best_price - offset if direction == "long" else best_price + offset


def reached(current: float, target: float, direction: str) -> bool:
    return current >= target if direction == "long" else current <= target


def sl_improved(old_sl: float, new_sl: float, direction: str) -> bool:
    """True if new_sl is a tighter protection than old_sl for the given dir."""
    return new_sl > old_sl if direction == "long" else new_sl < old_sl
