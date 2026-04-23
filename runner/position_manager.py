"""Live-position lifecycle manager.

Called every monitor tick. Responsibilities:

 1. Detect when a limit entry actually fills and attach SL + TP
    immediately. If attachment fails PROTECTION_MAX_RETRY times,
    EMERGENCY-CLOSE the position at market.
 2. On every tick, verify the SL and TP order ids are still alive on
    the exchange. If one has vanished (canceled / rejected / partial),
    re-attach it; if re-attach also fails, emergency close.
 3. Partial take profit: when price reaches TP1 (1R by default), close
    TP1_PORTION (50%) at market and — if BREAKEVEN_AFTER_TP1 — cancel
    the initial SL and repost it at the entry price for the remainder.
 4. Trailing stop: once price has moved at least TRAIL_ACTIVATION_RR in
    favor, maintain a new SL at (best_price ± TRAIL_DISTANCE_R × R).
    Only moves the SL tighter, never looser.
 5. When the live position disappears (hit TP, SL, or external close),
    drop tracking.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from config import (
    BREAKEVEN_AFTER_TP1,
    PROTECTION_MAX_RETRY,
    TP1_PORTION,
    TP2_PORTION,
    TRAIL_ENABLED,
)
from exchange.gateio import GateioFutures
from risk.exit_plan import (
    reached,
    risk_unit,
    sl_improved,
    tp1_price,
    tp2_price,
    trail_activation_price,
    trailing_sl,
)
from risk.sizing import PositionPlan
from runner.executor import EntryReceipt, Executor
from utils.logger import get_logger

log = get_logger("position")


@dataclass
class TrackedPosition:
    symbol: str
    direction: str            # "long" | "short"
    entry: float
    initial_sl: float
    current_sl: float
    tp: float
    qty_original: float       # contracts
    qty_remaining: float
    entry_order_id: str
    sl_order_id: Optional[str] = None
    tp_order_id: Optional[str] = None
    opened: bool = False
    tp1_done: bool = False
    tp2_done: bool = False
    breakeven_moved: bool = False
    best_price: Optional[float] = None
    attach_retries: int = 0
    closed: bool = False
    reason_closed: str = ""


class PositionManager:
    def __init__(self, ex: GateioFutures, executor: Executor) -> None:
        self._ex = ex
        self._exec = executor
        self._tracked: Dict[str, TrackedPosition] = {}

    # ------------------------------------------------------------------ api
    def register(self, receipt: EntryReceipt, plan: PositionPlan) -> TrackedPosition:
        t = TrackedPosition(
            symbol=plan.symbol,
            direction=plan.direction,
            entry=plan.entry,
            initial_sl=plan.sl,
            current_sl=plan.sl,
            tp=plan.tp,
            qty_original=plan.qty_contracts,
            qty_remaining=plan.qty_contracts,
            entry_order_id=receipt.order_id,
        )
        self._tracked[plan.symbol] = t
        return t

    def tracked(self) -> List[TrackedPosition]:
        return list(self._tracked.values())

    def active_symbols(self) -> set[str]:
        return {s for s, t in self._tracked.items() if not t.closed}

    # ----------------------------------------------------------------- tick
    async def tick(self) -> None:
        try:
            live_list = await self._ex.positions()
        except Exception as exc:
            log.warning("positions() failed: %s", exc)
            return
        live_by_sym: Dict[str, Dict[str, Any]] = {p["symbol"]: p for p in live_list}

        # 1) handle tracked positions
        for sym in list(self._tracked):
            t = self._tracked[sym]
            if t.closed:
                self._tracked.pop(sym, None)
                continue
            live = live_by_sym.get(sym)
            await self._tick_one(t, live)

        # 2) ORPHAN DETECTION — a live position we are not tracking means
        #    this process did not place it (e.g. a previous crashed run).
        #    An orphan without SL = the very bug that caused a 24 h-hold
        #    liquidation. Close it immediately.
        for sym, pos in live_by_sym.items():
            if sym in self._tracked:
                continue
            qty = float(pos.get("contracts") or 0)
            if qty <= 0:
                continue
            try:
                opens = await self._ex.open_orders(sym)
            except Exception as exc:
                log.warning("orphan %s: open_orders failed — closing anyway: %s", sym, exc)
                opens = []
            has_sl = any(o.get("stopPrice") for o in opens)
            if has_sl:
                continue   # some SL exists — leave it alone (user may have placed manually)
            side = (pos.get("side") or "").lower()
            direction = "long" if side in ("long", "buy") else "short"
            log.error("ORPHAN position %s qty=%.6g with NO SL — emergency closing", sym, qty)
            try:
                await self._exec.emergency_close(sym, direction, qty)
            except Exception as exc:
                log.exception("orphan close FAILED %s: %s", sym, exc)

    async def _tick_one(self, t: TrackedPosition, live: Optional[Dict[str, Any]]) -> None:
        if live is None:
            if t.opened:
                log.info("position closed externally / by TP or SL: %s", t.symbol)
                self._drop(t, "vanished")
            return

        qty_live = float(live.get("contracts") or 0)
        if qty_live <= 0:
            if t.opened:
                self._drop(t, "flat")
            return

        if not t.opened:
            t.opened = True
            t.qty_remaining = qty_live
            t.best_price = t.entry
            log.info("entry filled %s qty=%.6g — attaching protection", t.symbol, qty_live)

        # ---- 1. protection guard -------------------------------------------
        if not await self._ensure_protected(t):
            return  # either retried or emergency-closed; come back next tick

        # ---- 2. multi-tier partial TP --------------------------------------
        await self._check_partial_tp(t, live)
        await self._check_partial_tp2(t, live)

        # ---- 3. trailing (runner only) -------------------------------------
        await self._check_trailing(t, live)

    # ----------------------------------------------------------- protection
    async def _ensure_protected(self, t: TrackedPosition) -> bool:
        """Verify BY PRESENCE (not by id): an SL must exist as a stopPrice
        order, a TP as a reduce-only limit. Attempt one attach for each
        missing piece, then RE-VERIFY. If anything is still missing after
        the re-verify, emergency-close immediately — we never assume the
        attach that just 'returned ok' really landed."""
        try:
            opens = await self._ex.open_orders(t.symbol)
        except Exception as exc:
            log.warning("open_orders(%s) failed: %s", t.symbol, exc)
            return False

        has_sl = any(o.get("stopPrice") for o in opens)
        has_tp = any(
            o.get("reduceOnly") and not o.get("stopPrice") and o.get("price")
            for o in opens
        )

        if not has_sl:
            if not await self._attach(t, kind="sl"):
                return False
        if not has_tp:
            if not await self._attach(t, kind="tp"):
                return False

        if not has_sl or not has_tp:
            # we tried to attach — re-query and confirm it actually landed.
            try:
                opens2 = await self._ex.open_orders(t.symbol)
            except Exception as exc:
                log.warning("re-verify open_orders(%s) failed: %s", t.symbol, exc)
                return False
            has_sl2 = any(o.get("stopPrice") for o in opens2)
            has_tp2 = any(
                o.get("reduceOnly") and not o.get("stopPrice") and o.get("price")
                for o in opens2
            )
            if not (has_sl2 and has_tp2):
                log.error(
                    "post-attach verify FAILED %s (sl=%s tp=%s) — emergency close",
                    t.symbol, has_sl2, has_tp2,
                )
                await self._emergency_close(t, "post-attach verify failed")
                return False

        t.attach_retries = 0
        return True

    async def _attach(self, t: TrackedPosition, *, kind: str) -> bool:
        try:
            if kind == "sl":
                t.sl_order_id = await self._exec.attach_stop_loss(
                    t.symbol, t.direction, t.qty_remaining, t.current_sl
                )
            else:
                t.tp_order_id = await self._exec.attach_take_profit(
                    t.symbol, t.direction, t.qty_remaining, t.tp
                )
            return True
        except Exception as exc:
            t.attach_retries += 1
            log.error(
                "%s attach failed for %s (%d/%d): %s",
                kind.upper(),
                t.symbol,
                t.attach_retries,
                PROTECTION_MAX_RETRY,
                exc,
            )
            if t.attach_retries >= PROTECTION_MAX_RETRY:
                await self._emergency_close(t, f"{kind} attach exhausted")
            return False

    async def _emergency_close(self, t: TrackedPosition, reason: str) -> None:
        try:
            await self._exec.emergency_close(t.symbol, t.direction, t.qty_remaining)
        except Exception as exc:
            log.exception("EMERGENCY CLOSE ALSO FAILED %s: %s", t.symbol, exc)
        self._drop(t, f"emergency: {reason}")

    def _drop(self, t: TrackedPosition, reason: str) -> None:
        t.closed = True
        t.reason_closed = reason
        log.info("dropping %s (%s)", t.symbol, reason)
        self._tracked.pop(t.symbol, None)

    # ------------------------------------------------------------ partial TPs
    async def _check_partial_tp(self, t: TrackedPosition, live: Dict[str, Any]) -> None:
        """First tier: close TP1_PORTION of original qty at 1R, then BE."""
        if t.tp1_done:
            return
        mark = _mark_price(live, fallback=t.entry)
        target = tp1_price(t.entry, t.initial_sl, t.direction)
        if not reached(mark, target, t.direction):
            return
        # use qty_ORIGINAL so the fraction is unambiguous across tiers
        partial = min(t.qty_original * TP1_PORTION, t.qty_remaining)
        if partial <= 0:
            return
        try:
            await self._exec.partial_close(t.symbol, t.direction, partial)
        except Exception as exc:
            log.warning("TP1 partial close failed %s: %s", t.symbol, exc)
            return
        t.qty_remaining -= partial
        t.tp1_done = True
        log.info("TP1 filled %s qty=%.6g remaining=%.6g", t.symbol, partial, t.qty_remaining)

        if BREAKEVEN_AFTER_TP1 and not t.breakeven_moved:
            try:
                t.sl_order_id = await self._exec.replace_stop_loss(
                    t.symbol, t.direction, t.qty_remaining, t.sl_order_id, t.entry
                )
                t.current_sl = t.entry
                t.breakeven_moved = True
                log.info("SL moved to break-even %s @ %.6g", t.symbol, t.entry)
            except Exception as exc:
                log.error("BE move failed %s: %s — flagging for emergency", t.symbol, exc)
                await self._emergency_close(t, "BE move failed")

    async def _check_partial_tp2(self, t: TrackedPosition, live: Dict[str, Any]) -> None:
        """Second tier: at TP2_RR (default 2R) close TP2_PORTION of original qty.
        SL stays at break-even, runner is what's left."""
        if not t.tp1_done or t.tp2_done or t.qty_remaining <= 0:
            return
        mark = _mark_price(live, fallback=t.entry)
        target = tp2_price(t.entry, t.initial_sl, t.direction)
        if not reached(mark, target, t.direction):
            return
        partial = min(t.qty_original * TP2_PORTION, t.qty_remaining)
        if partial <= 0:
            return
        try:
            await self._exec.partial_close(t.symbol, t.direction, partial)
        except Exception as exc:
            log.warning("TP2 partial close failed %s: %s", t.symbol, exc)
            return
        t.qty_remaining -= partial
        t.tp2_done = True
        log.info("TP2 filled %s qty=%.6g remaining=%.6g (runner)", t.symbol, partial, t.qty_remaining)
        # resize the SL so its qty matches the remaining runner
        try:
            t.sl_order_id = await self._exec.replace_stop_loss(
                t.symbol, t.direction, t.qty_remaining, t.sl_order_id, t.current_sl
            )
        except Exception as exc:
            log.warning("SL resize after TP2 failed %s: %s", t.symbol, exc)

    # ------------------------------------------------------------- trailing
    async def _check_trailing(self, t: TrackedPosition, live: Dict[str, Any]) -> None:
        if not TRAIL_ENABLED or t.qty_remaining <= 0:
            return
        mark = _mark_price(live, fallback=t.entry)
        if t.best_price is None:
            t.best_price = mark
        else:
            t.best_price = max(t.best_price, mark) if t.direction == "long" else min(t.best_price, mark)

        activation = trail_activation_price(t.entry, t.initial_sl, t.direction)
        if not reached(t.best_price, activation, t.direction):
            return

        new_sl = trailing_sl(t.best_price, t.entry, t.initial_sl, t.direction)
        if not sl_improved(t.current_sl, new_sl, t.direction):
            return

        try:
            t.sl_order_id = await self._exec.replace_stop_loss(
                t.symbol, t.direction, t.qty_remaining, t.sl_order_id, new_sl
            )
            old, t.current_sl = t.current_sl, new_sl
            log.info("trail SL %s %.6g -> %.6g (best=%.6g)", t.symbol, old, new_sl, t.best_price)
        except Exception as exc:
            log.warning("trail move failed %s: %s", t.symbol, exc)


def _mark_price(live: Dict[str, Any], *, fallback: float) -> float:
    for key in ("markPrice", "lastPrice", "entryPrice"):
        v = live.get(key)
        if v:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return fallback
