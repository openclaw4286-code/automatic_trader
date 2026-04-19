"""Trade-manager pipeline tests (everything mocked — no network)."""
from __future__ import annotations

import asyncio
import time
from datetime import time as dtime
from typing import List
from unittest.mock import patch

from ict.models import Bias, Signal, TFAnalysis
from llm.gate import LLMGate, Verdict
from risk.sizing import PositionPlan
from runner.executor import EntryReceipt
from runner.manager import TradeManager
from runner.position_manager import PositionManager


# ---------------------- fakes ------------------------------------------
class _FakeExchange:
    def __init__(self, equity: float = 10_000.0, positions: List[dict] | None = None) -> None:
        self._equity = equity
        self._positions = positions or []
        self._markets = {
            "BTC/USDT:USDT": {
                "contractSize": 0.0001,
                "precision": {"amount": 0.0001},
                "limits": {"amount": {"min": 0.0001}},
            },
            "ETH/USDT:USDT": {
                "contractSize": 0.01,
                "precision": {"amount": 0.01},
                "limits": {"amount": {"min": 0.01}},
            },
        }

    async def equity_usdt(self) -> float:
        return self._equity

    async def positions(self) -> list:
        return list(self._positions)


class _FakeExec:
    def __init__(self) -> None:
        self.placed: List[PositionPlan] = []

    async def place_entry(self, plan: PositionPlan) -> EntryReceipt:
        self.placed.append(plan)
        return EntryReceipt(order_id=f"e-{plan.symbol}", symbol=plan.symbol, plan=plan)

    async def cancel_stale(self, pending, now):
        return []


def _sig(symbol: str, entry=100.0, sl=99.0, tp=102.0, direction="long") -> Signal:
    tf = TFAnalysis(tf="X", bias=Bias.BULL if direction == "long" else Bias.BEAR, last_close=entry)
    return Signal(symbol=symbol, direction=direction, entry=entry, sl=sl, tp=tp,
                  reason="t", htf=tf, mtf=tf, ltf=tf)


def _gate(status: str) -> LLMGate:
    g = LLMGate.__new__(LLMGate)
    g._verdict = Verdict(status=status, reason="t", decided_at=time.time(), expires_at=time.time() + 600)
    return g


def _gate_pass() -> LLMGate:
    return _gate("PASS")


def _gate_wait() -> LLMGate:
    return _gate("WAIT")


# ---------------------- tests ------------------------------------------
def test_wait_verdict_blocks_every_signal():
    async def _run():
        ex = _FakeExchange()
        mgr = TradeManager(ex, _gate_wait(), _FakeExec())
        with patch("runner.manager.in_session", return_value=True):
            placed = await mgr.process([_sig("BTC/USDT:USDT"), _sig("ETH/USDT:USDT", entry=2000, sl=1980, tp=2050)])
        assert placed == []

    asyncio.run(_run())


def test_out_of_session_blocks_every_signal():
    async def _run():
        ex = _FakeExchange()
        mgr = TradeManager(ex, _gate_pass(), _FakeExec())
        with patch("runner.manager.in_session", return_value=False):
            placed = await mgr.process([_sig("BTC/USDT:USDT")])
        assert placed == []

    asyncio.run(_run())


def test_pass_in_session_places_orders():
    async def _run():
        ex = _FakeExchange()
        execu = _FakeExec()
        mgr = TradeManager(ex, _gate_pass(), execu)
        with patch("runner.manager.in_session", return_value=True):
            placed = await mgr.process([_sig("BTC/USDT:USDT")])
        assert len(placed) == 1
        assert execu.placed[0].symbol == "BTC/USDT:USDT"
        assert execu.placed[0].side == "buy"

    asyncio.run(_run())


def test_duplicate_symbol_is_skipped_if_already_live():
    async def _run():
        ex = _FakeExchange(positions=[{"symbol": "BTC/USDT:USDT", "contracts": 1}])
        execu = _FakeExec()
        mgr = TradeManager(ex, _gate_pass(), execu)
        with patch("runner.manager.in_session", return_value=True):
            placed = await mgr.process([_sig("BTC/USDT:USDT")])
        assert placed == []

    asyncio.run(_run())


def test_position_cap_stops_new_entries():
    async def _run():
        live = [{"symbol": f"X{i}/USDT:USDT", "contracts": 1} for i in range(10)]
        ex = _FakeExchange(positions=live)
        execu = _FakeExec()
        mgr = TradeManager(ex, _gate_pass(), execu)
        with patch("runner.manager.in_session", return_value=True):
            placed = await mgr.process([_sig("BTC/USDT:USDT")])
        assert placed == []
        assert execu.placed == []

    asyncio.run(_run())


def test_multiple_signals_respect_intra_cycle_dedupe():
    async def _run():
        ex = _FakeExchange()
        execu = _FakeExec()
        mgr = TradeManager(ex, _gate_pass(), execu)
        sigs = [_sig("BTC/USDT:USDT"), _sig("BTC/USDT:USDT"), _sig("ETH/USDT:USDT", entry=2000, sl=1980, tp=2050)]
        with patch("runner.manager.in_session", return_value=True):
            with patch("runner.manager.in_pre_weekend_freeze", return_value=False):
                placed = await mgr.process(sigs)
        symbols = sorted([p.symbol for p in execu.placed])
        assert symbols == ["BTC/USDT:USDT", "ETH/USDT:USDT"]
        assert len(placed) == 2

    asyncio.run(_run())


def test_long_only_blocks_shorts_and_halves_long_size():
    async def _run():
        ex = _FakeExchange()
        execu = _FakeExec()
        mgr = TradeManager(ex, _gate("LONG_ONLY"), execu)
        sigs = [
            _sig("BTC/USDT:USDT", direction="long"),
            _sig("ETH/USDT:USDT", entry=2000, sl=1980, tp=2050, direction="short"),
        ]
        with patch("runner.manager.in_session", return_value=True):
            with patch("runner.manager.in_pre_weekend_freeze", return_value=False):
                placed = await mgr.process(sigs)
        # long accepted (half size), short blocked
        assert len(placed) == 1
        assert placed[0].symbol == "BTC/USDT:USDT"
        # half-size proof: expected_loss at half of RISK_PER_TRADE * equity
        from config import RISK_PER_TRADE
        expected = ex._equity * RISK_PER_TRADE * 0.5
        assert abs(execu.placed[0].expected_loss_usdt - expected) / expected < 0.05

    asyncio.run(_run())


def test_pre_weekend_freeze_blocks_new_entries():
    async def _run():
        ex = _FakeExchange()
        execu = _FakeExec()
        mgr = TradeManager(ex, _gate_pass(), execu)
        with patch("runner.manager.in_session", return_value=True):
            with patch("runner.manager.in_pre_weekend_freeze", return_value=True):
                placed = await mgr.process([_sig("BTC/USDT:USDT")])
        assert placed == []

    asyncio.run(_run())


def test_directional_verdict_halves_position_cap():
    async def _run():
        # 5 live positions is below the default cap (10) but at/over the halved
        # cap (5) once LLM goes directional — should refuse new entries.
        live = [{"symbol": f"X{i}/USDT:USDT", "contracts": 1} for i in range(5)]
        ex = _FakeExchange(positions=live)
        execu = _FakeExec()
        mgr = TradeManager(ex, _gate("LONG_ONLY"), execu)
        with patch("runner.manager.in_session", return_value=True):
            with patch("runner.manager.in_pre_weekend_freeze", return_value=False):
                placed = await mgr.process([_sig("BTC/USDT:USDT", direction="long")])
        assert placed == []

    asyncio.run(_run())


if __name__ == "__main__":
    test_wait_verdict_blocks_every_signal()
    test_out_of_session_blocks_every_signal()
    test_pass_in_session_places_orders()
    test_duplicate_symbol_is_skipped_if_already_live()
    test_position_cap_stops_new_entries()
    test_multiple_signals_respect_intra_cycle_dedupe()
    test_long_only_blocks_shorts_and_halves_long_size()
    test_pre_weekend_freeze_blocks_new_entries()
    test_directional_verdict_halves_position_cap()
    print("all manager tests passed")
