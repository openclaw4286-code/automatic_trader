"""Trade manager — chains scanner output through every gate and into orders.

Pipeline per cycle (only when we are in-session):
 signals  → LLM gate PASS  → portfolio caps  → per-symbol sizing
          → executor.place(plan).

Each step can drop candidates; the manager records pending entries so
they do not double-fire across cycles.
"""
from __future__ import annotations

import time
from typing import Dict, List

from exchange.gateio import GateioFutures
from ict.models import Signal
from llm.gate import LLMGate
from risk.portfolio import already_in_symbol, can_open_new
from risk.sizing import plan_position
from runner.executor import Executor, PlacedOrders
from utils.logger import get_logger
from utils.session import in_session

log = get_logger("manager")


class TradeManager:
    def __init__(self, ex: GateioFutures, gate: LLMGate, executor: Executor | None = None) -> None:
        self._ex = ex
        self._gate = gate
        self._exec = executor or Executor(ex)
        self._pending: Dict[str, Dict] = {}          # order_id -> {symbol, placed_at}
        self._active_symbols: set[str] = set()       # entries we already placed this cycle

    async def process(self, signals: List[Signal]) -> List[PlacedOrders]:
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

        market_table = self._ex._markets  # loaded at startup
        equity = await self._ex.equity_usdt()

        placed: List[PlacedOrders] = []
        for sig in signals:
            if already_in_symbol(live, sig.symbol) or sig.symbol in self._active_symbols:
                continue
            if not can_open_new([*live, *[{"symbol": s} for s in self._active_symbols]]):
                break
            market = market_table.get(sig.symbol)
            if market is None:
                continue
            plan = plan_position(sig, equity, market)
            if plan is None:
                continue
            try:
                orders = await self._exec.place(plan)
            except Exception as exc:
                log.warning("order placement failed %s: %s", sig.symbol, exc)
                continue
            self._pending[orders.entry_id] = {
                "symbol": sig.symbol,
                "placed_at": time.time(),
            }
            self._active_symbols.add(sig.symbol)
            placed.append(orders)

        return placed

    async def cleanup_stale(self) -> None:
        killed = await self._exec.cancel_stale(self._pending, time.time())
        for oid in killed:
            meta = self._pending.pop(oid, None)
            if meta:
                self._active_symbols.discard(meta["symbol"])
