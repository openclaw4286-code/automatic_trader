"""Trade manager — chains scanner output through every gate into entries.

Pipeline per cycle (only when we are in-session):
 signals  → LLM gate PASS  → portfolio caps (incl. tracked)
          → per-symbol sizing  → executor.place_entry(plan)
          → position_manager.register(receipt, plan)

Immediately after a successful entry the manager spawns a verification
task that waits ENTRY_VERIFY_DELAY_SEC, then checks that both SL and
TP are alive on the exchange — if either is missing, the position is
market-closed right away. This is the primary defence against the
24-hour orphan-position scenario that led to a full liquidation.

Partial TP, trailing, and ongoing protection guarding are owned by
PositionManager and run on the monitor loop tick.
"""
from __future__ import annotations

import asyncio
import time
from typing import Dict, List

from config import (
    ENTRY_VERIFY_DELAY_SEC,
    MAX_CONCURRENT_POSITIONS,
    SAME_DIRECTION_COOLDOWN_MIN,
)
from exchange.gateio import GateioFutures
from ict.models import Signal
from llm.gate import LLMGate
from risk.portfolio import already_in_symbol
from risk.sizing import plan_position
from runner.executor import EntryReceipt, Executor
from runner.position_manager import PositionManager
from utils.logger import get_logger
from utils.session import in_pre_weekend_freeze, in_session

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
        # Per-direction cooldown: (symbol, direction) -> last_entry_epoch_sec.
        # Blocks the bot from spamming the same persistent ICT setup.
        self._recent_entries: Dict[tuple[str, str], float] = {}

    @property
    def position_manager(self) -> PositionManager:
        return self._pos

    async def process(self, signals: List[Signal]) -> List[EntryReceipt]:
        if not signals:
            return []
        if not in_session():
            log.debug("outside session — skipping %d signals", len(signals))
            return []
        if in_pre_weekend_freeze():
            log.info("pre-weekend freeze — %d signals skipped", len(signals))
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
        tracked_syms = self._pos.active_symbols()
        effective_cap = self._gate.position_cap(MAX_CONCURRENT_POSITIONS)
        total_open = len(live) + len(tracked_syms - {p.get("symbol") for p in live})
        if total_open >= effective_cap:
            log.info(
                "position cap hit (%d/%d, LLM=%s) — skipping new entries",
                total_open,
                effective_cap,
                self._gate.verdict.status if self._gate.verdict else "-",
            )
            return []

        market_table = self._ex._markets
        equity = await self._ex.equity_usdt()

        placed: List[EntryReceipt] = []
        for sig in signals:
            if already_in_symbol(live, sig.symbol) or sig.symbol in tracked_syms:
                continue
            key = (sig.symbol, sig.direction)
            last_ts = self._recent_entries.get(key)
            if last_ts is not None:
                elapsed = time.time() - last_ts
                if elapsed < SAME_DIRECTION_COOLDOWN_MIN * 60:
                    remaining = SAME_DIRECTION_COOLDOWN_MIN * 60 - elapsed
                    log.info(
                        "%s %s in cooldown (%.1f min left) — skipping",
                        sig.symbol, sig.direction, remaining / 60,
                    )
                    continue
            if not self._gate.allow_direction(sig.direction):
                log.info(
                    "LLM blocks direction=%s for %s (verdict=%s)",
                    sig.direction,
                    sig.symbol,
                    self._gate.verdict.status if self._gate.verdict else "-",
                )
                continue
            scale = self._gate.size_scale(sig.direction)
            if scale <= 0:
                continue
            # Re-check cap before EACH entry (we may have placed some in this cycle)
            open_count = len(live) + len(tracked_syms)
            if open_count >= effective_cap:
                break
            market = market_table.get(sig.symbol)
            if market is None:
                continue
            plan = plan_position(sig, equity, market, risk_scale=scale, margin_scale=scale)
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
            self._recent_entries[key] = time.time()
            tracked_syms.add(sig.symbol)
            placed.append(receipt)
            # Fire-and-forget verification: if SL or TP did not land on
            # the exchange, close the position at market right away.
            asyncio.create_task(self._verify_protection(plan))

        return placed

    async def _verify_protection(self, plan) -> None:
        """Wait briefly then confirm that SL and TP orders exist on the
        exchange for `plan.symbol`. If either is missing AND the
        position is live, emergency-close immediately."""
        try:
            await asyncio.sleep(ENTRY_VERIFY_DELAY_SEC)
            live = await self._ex.positions()
            pos = next((p for p in live if p.get("symbol") == plan.symbol), None)
            if pos is None or float(pos.get("contracts") or 0) <= 0:
                return  # entry not filled yet — nothing to guard
            opens = await self._ex.open_orders(plan.symbol)
            has_sl = any(o.get("stopPrice") for o in opens)
            has_tp = any(
                o.get("reduceOnly") and not o.get("stopPrice") and o.get("price")
                for o in opens
            )
            if has_sl and has_tp:
                return
            qty = float(pos.get("contracts") or 0)
            log.error(
                "post-entry verify FAILED %s (sl=%s tp=%s) — emergency close qty=%.6g",
                plan.symbol, has_sl, has_tp, qty,
            )
            await self._exec.emergency_close(plan.symbol, plan.direction, qty)
            # drop tracking so the monitor does not keep re-attaching
            self._pos._tracked.pop(plan.symbol, None)   # type: ignore[attr-defined]
        except Exception as exc:
            log.exception("post-entry verify crashed for %s: %s", plan.symbol, exc)

    async def cleanup_stale(self) -> None:
        # Entries whose position opened are no longer pending — drop them
        # immediately so cleanup_stale does not keep trying to cancel a
        # filled entry id (Gate returns AUTO_ORDER_NOT_FOUND).
        opened = {t.symbol for t in self._pos.tracked() if t.opened}
        for oid, meta in list(self._pending.items()):
            if meta["symbol"] in opened:
                self._pending.pop(oid, None)

        killed = await self._exec.cancel_stale(self._pending, time.time())
        for oid in killed:
            meta = self._pending.pop(oid, None)
            if not meta:
                continue
            tracked = self._pos._tracked.get(meta["symbol"])  # type: ignore[attr-defined]
            if tracked and not tracked.opened:
                self._pos._tracked.pop(meta["symbol"], None)  # type: ignore[attr-defined]
