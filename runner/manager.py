"""Trade manager — chains scanner output through every gate into entries.

Pipeline per cycle (only when we are in-session):
 signals  → LLM gate PASS  → portfolio caps (incl. tracked)
          → per-symbol sizing  → executor.place_entry(plan)
          → position_manager.register(receipt, plan)

Protection (SL + TP), partial TP, trailing, and emergency close are
owned by PositionManager and run on the monitor loop tick.
"""
from __future__ import annotations

import time
from typing import Dict, List

from exchange.gateio import GateioFutures
from ict.models import Signal
from llm.gate import LLMGate
from risk.portfolio import already_in_symbol, can_open_new
from risk.sizing import plan_position
from runner.executor import EntryReceipt, Executor
from runner.position_manager import PositionManager
from utils.logger import get_logger
from utils.session import in_session

log = get_logger("manager")


class TradeManager:
    def __init__(
        self,
        ex: GateioFutures,
        gate: LLMGate,
        executor: Executor | None = None,
        position_manager: PositionManager | None = None,
    ) -> None:
        self._ex = ex
        self._gate = gate
        self._exec = executor or Executor(ex)
        self._pos = position_manager or PositionManager(ex, self._exec)
        self._pending: Dict[str, Dict] = {}   # entry_order_id -> {symbol, placed_at}

    @property
    def position_manager(self) -> PositionManager:
        return self._pos

    async def process(self, signals: List[Signal]) -> List[EntryReceipt]:
        if not signals:
            return []
        if not in_session():
            log.debug("outside session — skipping %d signals", len(signals))
            return []
        if not self._gate.allow_trades():
            v = self._gate.verdict
            log.info(
                "LLM gate blocked (%s: %s) — %d signals skipped",
                v.status if v else "no-verdict",
                (v.reason if v else "")[:80],
                len(signals),
            )
            return []

        live = await self._ex.positions()
        if not can_open_new(live):
            return []

        tracked_syms = self._pos.active_symbols()
        market_table = self._ex._markets
        equity = await self._ex.equity_usdt()

        placed: List[EntryReceipt] = []
        for sig in signals:
            if already_in_symbol(live, sig.symbol) or sig.symbol in tracked_syms:
                continue
            if not can_open_new([*live, *[{"symbol": s} for s in tracked_syms]]):
                break
            market = market_table.get(sig.symbol)
            if market is None:
                continue
            plan = plan_position(sig, equity, market)
            if plan is None:
                continue
            try:
                receipt = await self._exec.place_entry(plan)
            except Exception as exc:
                log.warning("entry placement failed %s: %s", sig.symbol, exc)
                continue
            self._pos.register(receipt, plan)
            self._pending[receipt.order_id] = {
                "symbol": sig.symbol,
                "placed_at": time.time(),
            }
            tracked_syms.add(sig.symbol)
            placed.append(receipt)

        return placed

    async def cleanup_stale(self) -> None:
        killed = await self._exec.cancel_stale(self._pending, time.time())
        for oid in killed:
            meta = self._pending.pop(oid, None)
            if not meta:
                continue
            # also drop any PositionManager tracking for that symbol IF it
            # never opened (entry never filled — nothing to guard).
            tracked = self._pos._tracked.get(meta["symbol"])  # type: ignore[attr-defined]
            if tracked and not tracked.opened:
                self._pos._tracked.pop(meta["symbol"], None)  # type: ignore[attr-defined]
