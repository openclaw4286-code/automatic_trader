"""Force a single end-to-end trade lifecycle in DRY_RUN for verification.

What you get to watch in one minute instead of waiting for a real ICT
setup:
  * PositionPlan computation (qty / leverage / margin / expected_loss)
  * entry fill in the paper broker
  * SL + TP attachment to the "live" position
  * optional: TP1 partial close + break-even move
  * optional: trailing stop tightening on simulated price drift
  * either a TP / SL hit or an emergency close

Usage:
  python scripts/simulate_trade.py                       # default BTC long
  python scripts/simulate_trade.py --symbol ETH/USDT:USDT --direction short
  python scripts/simulate_trade.py --scenario tp    # price drifts to TP1+TP
  python scripts/simulate_trade.py --scenario sl    # price drifts into SL
  python scripts/simulate_trade.py --scenario trail # price runs favorably
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Dict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import DRY_RUN
from exchange.gateio import GateioFutures
from ict.models import Bias, Signal, TFAnalysis
from risk.sizing import plan_position
from runner.executor import Executor
from runner.position_manager import PositionManager


def build_signal(symbol: str, direction: str, entry: float, sl_pct: float, tp_pct: float) -> Signal:
    if direction == "long":
        sl = entry * (1 - sl_pct / 100)
        tp = entry * (1 + tp_pct / 100)
    else:
        sl = entry * (1 + sl_pct / 100)
        tp = entry * (1 - tp_pct / 100)
    tf = TFAnalysis(tf="X", bias=Bias.BULL if direction == "long" else Bias.BEAR, last_close=entry)
    return Signal(
        symbol=symbol,
        direction=direction,
        entry=entry,
        sl=sl,
        tp=tp,
        reason="simulated",
        htf=tf, mtf=tf, ltf=tf,
    )


class ScriptedTicker:
    """Replaces the real ticker fetcher on the exchange so we can drive mark
    price without waiting for the market."""

    def __init__(self, symbol: str, script: list[float]) -> None:
        self._symbol = symbol
        self._prices = list(script)

    async def __call__(self, symbol: str) -> Dict:
        if symbol != self._symbol or not self._prices:
            return {"last": 0.0}
        price = self._prices.pop(0) if len(self._prices) > 1 else self._prices[0]
        return {"last": price, "close": price}


def scenario_prices(scenario: str, entry: float, sl: float, tp: float, direction: str) -> list[float]:
    """Return a sequence of mark prices sampled every paper tick.

    Use generous distances past rounded triggers so the paper broker's
    tick-size-rounded SL/TP orders actually fire.
    """
    r = abs(entry - sl)
    tp1 = entry + r if direction == "long" else entry - r
    if scenario == "tp":
        if direction == "long":
            path = [entry, tp1 + r * 0.1, tp1 + r * 0.1, tp + r * 0.1, tp + r * 0.1]
        else:
            path = [entry, tp1 - r * 0.1, tp1 - r * 0.1, tp - r * 0.1, tp - r * 0.1]
    elif scenario == "sl":
        mid = (entry + sl) / 2
        # push well past the rounded stop so tick-rounding cannot hide the trigger
        past_sl = sl - r * 0.2 if direction == "long" else sl + r * 0.2
        path = [entry, mid, past_sl, past_sl]
    elif scenario == "trail":
        if direction == "long":
            path = [entry, tp1, tp1 + (tp - tp1) * 0.4, tp1 + (tp - tp1) * 0.2]
        else:
            path = [entry, tp1, tp1 - (tp1 - tp) * 0.4, tp1 - (tp1 - tp) * 0.2]
    else:
        path = [entry]
    return path


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTC/USDT:USDT")
    ap.add_argument("--direction", choices=["long", "short"], default="long")
    ap.add_argument("--sl-pct", type=float, default=0.5)
    ap.add_argument("--tp-pct", type=float, default=1.5)
    ap.add_argument("--scenario", choices=["hold", "tp", "sl", "trail"], default="tp")
    ap.add_argument("--ticks", type=int, default=8)
    args = ap.parse_args()

    if not DRY_RUN:
        print("refusing to run — DRY_RUN=false", file=sys.stderr)
        return 1

    ex = GateioFutures()
    await ex.load()

    tkr = await ex.ticker(args.symbol)
    entry = float(tkr.get("last") or tkr.get("close") or 0)
    if entry <= 0:
        print(f"could not fetch price for {args.symbol}", file=sys.stderr)
        await ex.close()
        return 1
    print(f"== using live price {args.symbol} = {entry:.6g}")

    sig = build_signal(args.symbol, args.direction, entry, args.sl_pct, args.tp_pct)
    equity = await ex.equity_usdt()
    print(f"== equity={equity:.2f} USDT  signal sl={sig.sl:.6g}  tp={sig.tp:.6g}  rr={sig.rr:.2f}")

    plan = plan_position(sig, equity, ex._markets.get(args.symbol) or {})
    if plan is None:
        print("plan_position returned None — equity too small for this SL distance", file=sys.stderr)
        await ex.close()
        return 1
    print("== plan:", plan.as_log())

    executor = Executor(ex)
    pm = PositionManager(ex, executor)

    receipt = await executor.place_entry(plan)
    pm.register(receipt, plan)

    # replace the real ticker call with a scripted one so prices drift deterministically
    script = scenario_prices(args.scenario, plan.entry, plan.sl, plan.tp, plan.direction)
    ex._fetch_ticker_real = ScriptedTicker(args.symbol, script)   # type: ignore[assignment]

    print(f"== driving {args.ticks} paper ticks, scenario={args.scenario}")
    for i in range(args.ticks):
        await ex.paper_tick()
        await pm.tick()
        positions = await ex.positions()
        if not positions and pm.active_symbols() == set():
            print(f"== position closed on tick {i+1}, stopping")
            break
        await asyncio.sleep(0.1)

    print(f"== realized pnl (paper): {ex._paper.realized_pnl:+.4f} USDT")   # type: ignore[union-attr]
    await ex.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
