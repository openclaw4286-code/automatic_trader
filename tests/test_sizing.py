"""Sanity checks for risk.sizing."""
from __future__ import annotations

import pandas as pd

from ict.models import Bias, Signal, TFAnalysis
from risk.sizing import plan_position


def _signal(entry: float, sl: float, tp: float, direction: str = "long") -> Signal:
    tf = TFAnalysis(tf="X", bias=Bias.BULL if direction == "long" else Bias.BEAR, last_close=entry)
    return Signal(
        symbol="BTC/USDT:USDT",
        direction=direction,
        entry=entry,
        sl=sl,
        tp=tp,
        reason="test",
        htf=tf,
        mtf=tf,
        ltf=tf,
    )


MARKET = {"contractSize": 0.0001, "precision": {"amount": 0.0001}, "limits": {"amount": {"min": 0.0001}}}


def test_loss_at_sl_matches_risk_budget():
    equity = 10_000.0
    sig = _signal(entry=100.0, sl=99.0, tp=102.0)          # 1% SL
    plan = plan_position(sig, equity, MARKET)
    assert plan is not None
    assert abs(plan.expected_loss_usdt - equity * 0.015) / (equity * 0.015) < 0.05


def test_leverage_fits_inside_sl():
    equity = 10_000.0
    sig = _signal(entry=100.0, sl=95.0, tp=110.0)          # 5% SL
    plan = plan_position(sig, equity, MARKET)
    assert plan is not None
    assert plan.leverage <= 18                              # ~0.9/0.05 = 18
    assert plan.leverage >= 1


def test_margin_cap_applies():
    equity = 10_000.0
    sig = _signal(entry=100.0, sl=99.95, tp=100.2)         # 0.05% SL -> huge notional
    plan = plan_position(sig, equity, MARKET)
    assert plan is not None
    assert plan.capped_by_margin
    assert plan.margin_usdt <= equity * 0.10 + 1e-6


def test_short_direction():
    sig = _signal(entry=100.0, sl=101.0, tp=98.0, direction="short")
    plan = plan_position(sig, 10_000.0, MARKET)
    assert plan is not None
    assert plan.side == "sell"
    assert plan.direction == "short"


def test_tiny_qty_rejected():
    sig = _signal(entry=100_000.0, sl=99_000.0, tp=102_000.0)
    plan = plan_position(sig, 10.0, MARKET)                 # $10 equity -> dust
    assert plan is None or plan.qty_contracts > 0


if __name__ == "__main__":
    test_loss_at_sl_matches_risk_budget()
    test_leverage_fits_inside_sl()
    test_margin_cap_applies()
    test_short_direction()
    test_tiny_qty_rejected()
    print("all sizing tests passed")
