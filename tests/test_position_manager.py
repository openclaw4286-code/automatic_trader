"""Exercise the PositionManager lifecycle without touching an exchange.

All exchange and executor calls are faked with counters and scripted
side-effects so every branch — protection attach, attach-retry,
emergency close, partial TP + break-even, trailing stop, natural close
— is deterministic.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from risk.sizing import PositionPlan
from runner.executor import EntryReceipt
from runner.position_manager import PositionManager, TrackedPosition


# --------------------------- fakes ------------------------------------
class _FakeExchange:
    def __init__(self) -> None:
        self.positions_queue: List[List[Dict[str, Any]]] = []
        self.open_orders_queue: List[List[Dict[str, Any]]] = []
        self._markets: Dict[str, Any] = {}

    async def positions(self):
        if self.positions_queue:
            return self.positions_queue.pop(0)
        return []

    async def open_orders(self, symbol: Optional[str] = None):
        if self.open_orders_queue:
            return self.open_orders_queue.pop(0)
        return []


class _FakeExecutor:
    def __init__(self, attach_side_effect: Optional[List[Any]] = None) -> None:
        self.attach_sl_calls: List[float] = []
        self.attach_tp_calls: List[float] = []
        self.partial_close_calls: List[float] = []
        self.emergency_calls: List[float] = []
        self.replace_sl_calls: List[float] = []
        self._attach_side = list(attach_side_effect or [])
        self._sl_counter = 0
        self._tp_counter = 0

    def _pop_side(self):
        if self._attach_side:
            v = self._attach_side.pop(0)
            if isinstance(v, Exception):
                raise v
            return v
        return None

    async def attach_stop_loss(self, symbol, direction, qty, sl):
        self.attach_sl_calls.append(sl)
        self._pop_side()
        self._sl_counter += 1
        return f"sl-{self._sl_counter}"

    async def attach_take_profit(self, symbol, direction, qty, tp):
        self.attach_tp_calls.append(tp)
        self._pop_side()
        self._tp_counter += 1
        return f"tp-{self._tp_counter}"

    async def replace_stop_loss(self, symbol, direction, qty, old_id, new_sl):
        self.replace_sl_calls.append(new_sl)
        self._sl_counter += 1
        return f"sl-{self._sl_counter}"

    async def partial_close(self, symbol, direction, qty):
        self.partial_close_calls.append(qty)

    async def emergency_close(self, symbol, direction, qty):
        self.emergency_calls.append(qty)


# ---------------------- plan & helpers --------------------------------
def _plan(symbol="BTC/USDT:USDT", entry=100.0, sl=99.0, tp=103.0, direction="long") -> PositionPlan:
    return PositionPlan(
        symbol=symbol,
        direction=direction,
        side="buy" if direction == "long" else "sell",
        entry=entry,
        sl=sl,
        tp=tp,
        qty_contracts=1000.0,
        qty_base=1.0,
        notional_usdt=100.0,
        leverage=10,
        margin_usdt=10.0,
        expected_loss_usdt=1.0,
        rr=3.0,
        capped_by_margin=False,
    )


def _receipt(plan: PositionPlan) -> EntryReceipt:
    return EntryReceipt(order_id="e-1", symbol=plan.symbol, plan=plan)


def _live(symbol="BTC/USDT:USDT", contracts=1000.0, mark=100.0) -> Dict[str, Any]:
    return {"symbol": symbol, "contracts": contracts, "markPrice": mark, "entryPrice": 100.0}


# -------------------------- tests -------------------------------------
def test_entry_fill_attaches_sl_and_tp_once():
    async def _run():
        ex = _FakeExchange()
        fe = _FakeExecutor()
        mgr = PositionManager(ex, fe)                         # type: ignore[arg-type]
        plan = _plan()
        mgr.register(_receipt(plan), plan)

        # tick 1: no live position yet (entry unfilled) — nothing happens
        ex.positions_queue = [[]]
        await mgr.tick()
        assert fe.attach_sl_calls == [] and fe.attach_tp_calls == []

        # tick 2: position appears, protection attached
        ex.positions_queue = [[_live()]]
        ex.open_orders_queue = [[]]                           # nothing attached yet
        await mgr.tick()
        assert fe.attach_sl_calls == [99.0]
        assert fe.attach_tp_calls == [103.0]

    asyncio.run(_run())


def test_missing_sl_is_reattached_on_next_tick():
    async def _run():
        ex = _FakeExchange()
        fe = _FakeExecutor()
        mgr = PositionManager(ex, fe)                         # type: ignore[arg-type]
        plan = _plan()
        mgr.register(_receipt(plan), plan)

        ex.positions_queue = [[_live()]]
        ex.open_orders_queue = [[]]                           # first attach
        await mgr.tick()
        assert fe.attach_sl_calls == [99.0]

        # next tick: SL id vanished (exchange rejected / filled partial)
        ex.positions_queue = [[_live()]]
        ex.open_orders_queue = [[{"id": "tp-1"}]]             # only TP survives
        await mgr.tick()
        assert len(fe.attach_sl_calls) == 2                   # re-attached

    asyncio.run(_run())


def test_attach_failures_trigger_emergency_close():
    async def _run():
        ex = _FakeExchange()
        fe = _FakeExecutor(attach_side_effect=[RuntimeError("reject"), RuntimeError("reject")])
        mgr = PositionManager(ex, fe)                         # type: ignore[arg-type]
        plan = _plan()
        mgr.register(_receipt(plan), plan)

        for _ in range(2):
            ex.positions_queue.append([_live()])
            ex.open_orders_queue.append([])
        for _ in range(2):
            await mgr.tick()

        assert len(fe.emergency_calls) == 1
        assert mgr.active_symbols() == set()

    asyncio.run(_run())


def test_partial_tp_triggers_and_moves_sl_to_breakeven():
    async def _run():
        ex = _FakeExchange()
        fe = _FakeExecutor()
        mgr = PositionManager(ex, fe)                         # type: ignore[arg-type]
        plan = _plan(entry=100.0, sl=99.0, tp=103.0)          # 1R = 1.0 -> TP1 = 101
        mgr.register(_receipt(plan), plan)

        # 1) entry fills at mark=100; attach SL/TP
        ex.positions_queue = [[_live(mark=100.0)]]
        ex.open_orders_queue = [[]]
        await mgr.tick()

        # 2) mark jumps to 101.5 -> TP1 reached, partial close + BE move
        ex.positions_queue = [[_live(mark=101.5)]]
        ex.open_orders_queue = [[{"id": "sl-1"}, {"id": "tp-1"}]]
        await mgr.tick()

        assert fe.partial_close_calls and fe.partial_close_calls[0] == 400.0   # TP1_PORTION 40%
        assert fe.replace_sl_calls and fe.replace_sl_calls[0] == 100.0          # BE

    asyncio.run(_run())


def test_tp2_partial_after_tp1():
    """After TP1 fires at 1R, TP2 fires at 2R for the next portion."""
    async def _run():
        ex = _FakeExchange()
        fe = _FakeExecutor()
        mgr = PositionManager(ex, fe)                         # type: ignore[arg-type]
        plan = _plan(entry=100.0, sl=99.0, tp=110.0)          # R=1
        mgr.register(_receipt(plan), plan)

        # 1) entry fills
        ex.positions_queue = [[_live(mark=100.0)]]
        ex.open_orders_queue = [[]]
        await mgr.tick()

        # 2) mark 101 -> TP1 fires (40% = 400 contracts)
        ex.positions_queue = [[_live(mark=101.0, contracts=1000)]]
        ex.open_orders_queue = [[{"id": "sl-1"}, {"id": "tp-1"}]]
        await mgr.tick()
        assert fe.partial_close_calls[-1] == 400.0

        # 3) mark 102 -> TP2 fires (30% = 300 contracts)
        ex.positions_queue = [[_live(mark=102.0, contracts=600)]]
        ex.open_orders_queue = [[{"id": "sl-2"}, {"id": "tp-1"}]]
        await mgr.tick()
        assert fe.partial_close_calls[-1] == 300.0

    asyncio.run(_run())


def test_trailing_stop_tightens_when_price_runs():
    async def _run():
        ex = _FakeExchange()
        fe = _FakeExecutor()
        mgr = PositionManager(ex, fe)                         # type: ignore[arg-type]
        plan = _plan(entry=100.0, sl=99.0, tp=110.0)          # R = 1.0
        mgr.register(_receipt(plan), plan)

        # attach
        ex.positions_queue = [[_live(mark=100.0)]]
        ex.open_orders_queue = [[]]
        await mgr.tick()

        # price jumps to 102.5 → activation (1.5R) reached, trail = 102.5 - 0.8R = 101.7
        ex.positions_queue = [[_live(mark=102.5)]]
        ex.open_orders_queue = [[{"id": "sl-1"}, {"id": "tp-1"}]]
        await mgr.tick()

        assert any(abs(p - 101.7) < 1e-6 for p in fe.replace_sl_calls), fe.replace_sl_calls

        # price dips back to 102 — SL must NOT loosen
        pre = list(fe.replace_sl_calls)
        ex.positions_queue = [[_live(mark=102.0)]]
        ex.open_orders_queue = [[{"id": "sl-2"}, {"id": "tp-1"}]]
        await mgr.tick()
        assert fe.replace_sl_calls == pre

    asyncio.run(_run())


def test_position_vanishing_drops_tracking():
    async def _run():
        ex = _FakeExchange()
        fe = _FakeExecutor()
        mgr = PositionManager(ex, fe)                         # type: ignore[arg-type]
        plan = _plan()
        mgr.register(_receipt(plan), plan)

        # open
        ex.positions_queue = [[_live()]]
        ex.open_orders_queue = [[]]
        await mgr.tick()
        assert mgr.active_symbols() == {"BTC/USDT:USDT"}

        # gone (TP or SL filled)
        ex.positions_queue = [[]]
        await mgr.tick()
        assert mgr.active_symbols() == set()

    asyncio.run(_run())


if __name__ == "__main__":
    test_entry_fill_attaches_sl_and_tp_once()
    test_missing_sl_is_reattached_on_next_tick()
    test_attach_failures_trigger_emergency_close()
    test_partial_tp_triggers_and_moves_sl_to_breakeven()
    test_tp2_partial_after_tp1()
    test_trailing_stop_tightens_when_price_runs()
    test_position_vanishing_drops_tracking()
    print("all position_manager tests passed")
