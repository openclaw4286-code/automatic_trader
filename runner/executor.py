"""Atomic order primitives for the trade pipeline.

The previous bundle-of-three-orders design was brittle: a partial
failure could leave a live position without a stop. This rewrite
exposes small, independently-retryable operations and lets the
PositionManager decide the sequencing:

 * place_entry             — submit a limit entry, return its id
 * attach_stop_loss        — reduce-only stop-market guarding a position
 * attach_take_profit      — reduce-only limit at the plan TP
 * replace_stop_loss       — cancel old SL and place a new one (BE/trail)
 * partial_close           — market reduce-only for a fraction of qty
 * emergency_close         — market reduce-only for the full remaining qty
 * cancel_stale            — sweep entry orders past their TTL
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List

from config import ORDER_TTL_SEC
from exchange.gateio import GateioFutures
from risk.sizing import PositionPlan
from utils.logger import get_logger

log = get_logger("executor")


@dataclass
class EntryReceipt:
    order_id: str
    symbol: str
    plan: PositionPlan


class Executor:
    def __init__(self, ex: GateioFutures) -> None:
        self._ex = ex

    # ---------- helpers ----------
    def _market(self, symbol: str) -> Dict[str, Any]:
        m = self._ex._markets.get(symbol) or {}
        return m

    def _round_price(self, symbol: str, price: float) -> float:
        """Round DOWN to the market's price tick. Uniformly treats the
        precision value as a TICK SIZE regardless of whether it is less
        than, equal to, or greater than 1 — ccxt 4.x returns tick size
        in the `precision` fields for Gate futures, so the previous
        `round(price, int(step))` branch was silently wrong for symbols
        whose step is an integer."""
        m = self._market(symbol)
        step = (m.get("precision") or {}).get("price")
        if step is None or step <= 0 or price <= 0:
            return price
        return math.floor(price / step) * step

    def _round_qty(self, symbol: str, qty: float) -> float:
        """Same tick-size treatment for amounts. For an alt whose
        amount step is 1 (integer contracts), this now correctly floors
        11.3 → 11 instead of the previous (buggy) 11.3."""
        m = self._market(symbol)
        step = (m.get("precision") or {}).get("amount")
        if step is None or step <= 0:
            return qty
        return math.floor(qty / step) * step

    @staticmethod
    def _close_side(direction: str) -> str:
        return "sell" if direction == "long" else "buy"

    # ---------- operations ----------
    async def place_entry(self, plan: PositionPlan) -> EntryReceipt:
        """Place the limit entry AND ask ccxt to attach SL+TP atomically
        via the stopLossPrice / takeProfitPrice params. This minimises the
        exchange-side gap where a position is live without protection."""
        await self._ex.set_leverage(plan.symbol, plan.leverage)
        price = self._round_price(plan.symbol, plan.entry)
        sl_px = self._round_price(plan.symbol, plan.sl)
        tp_px = self._round_price(plan.symbol, plan.tp)
        qty = self._round_qty(plan.symbol, plan.qty_contracts)
        order = await self._ex.create_order(
            symbol=plan.symbol,
            side=plan.side,
            amount=qty,
            price=price,
            params={
                "timeInForce": "GTC",
                "stopLossPrice": sl_px,
                "takeProfitPrice": tp_px,
            },
        )
        log.info("entry placed (with atomic SL=%g TP=%g): %s", sl_px, tp_px, plan.as_log())
        return EntryReceipt(order_id=str(order.get("id")), symbol=plan.symbol, plan=plan)

    async def attach_stop_loss(self, symbol: str, direction: str, qty_contracts: float, sl: float) -> str:
        """Position-attached SL via Gate's ``close=True`` (auto_size).

        The order closes WHATEVER the position size is at the moment the
        trigger fires — so partial TPs that reduce the position
        automatically shrink the SL coverage too. ``qty_contracts`` is
        still passed for exchanges that require an amount, but Gate
        ignores it for close-trigger orders."""
        side = self._close_side(direction)
        price = self._round_price(symbol, sl)
        qty = self._round_qty(symbol, qty_contracts)
        order = await self._ex.create_order(
            symbol=symbol,
            side=side,
            amount=qty,
            price=None,
            params={
                "reduceOnly": True,
                "stopPrice": price,
                "close": True,
            },
        )
        oid = str(order.get("id"))
        log.info("SL attached %s @ %.6g id=%s (close=True)", symbol, price, oid)
        return oid

    async def attach_take_profit(self, symbol: str, direction: str, qty_contracts: float, tp: float) -> str:
        """Position-attached TP — reduce-only limit that closes the
        entire remaining position when price reaches ``tp``."""
        side = self._close_side(direction)
        price = self._round_price(symbol, tp)
        qty = self._round_qty(symbol, qty_contracts)
        order = await self._ex.create_order(
            symbol=symbol,
            side=side,
            amount=qty,
            price=price,
            params={
                "reduceOnly": True,
                "close": True,
            },
        )
        oid = str(order.get("id"))
        log.info("TP attached %s @ %.6g id=%s (close=True)", symbol, price, oid)
        return oid

    async def replace_stop_loss(
        self,
        symbol: str,
        direction: str,
        qty_contracts: float,
        old_order_id: str | None,
        new_sl: float,
    ) -> str:
        if old_order_id:
            try:
                await self._ex.cancel_order(old_order_id, symbol)
            except Exception as exc:
                log.warning("cancel old SL %s failed: %s", old_order_id, exc)
        return await self.attach_stop_loss(symbol, direction, qty_contracts, new_sl)

    async def partial_close(self, symbol: str, direction: str, qty_contracts: float) -> Dict[str, Any]:
        side = self._close_side(direction)
        qty = self._round_qty(symbol, qty_contracts)
        if qty <= 0:
            raise ValueError(f"partial_close qty<=0 for {symbol}")
        order = await self._ex.create_order(
            symbol=symbol,
            side=side,
            amount=qty,
            price=None,
            params={"reduceOnly": True, "type": "market"},
        )
        log.info("partial close %s qty=%.6g", symbol, qty)
        return order

    async def emergency_close(self, symbol: str, direction: str, qty_contracts: float) -> Dict[str, Any]:
        side = self._close_side(direction)
        qty = self._round_qty(symbol, qty_contracts)
        order = await self._ex.create_order(
            symbol=symbol,
            side=side,
            amount=qty,
            price=None,
            params={"reduceOnly": True, "type": "market"},
        )
        log.error("EMERGENCY CLOSE %s qty=%.6g (SL/TP could not be attached)", symbol, qty)
        return order

    async def cancel_stale(self, pending: Dict[str, Dict[str, Any]], now_ts: float) -> List[str]:
        killed: List[str] = []
        for order_id, meta in list(pending.items()):
            if now_ts - meta["placed_at"] < ORDER_TTL_SEC:
                continue
            try:
                await self._ex.cancel_order(order_id, meta["symbol"])
                killed.append(order_id)
                log.info("cancelled stale entry %s (%s)", order_id, meta["symbol"])
            except Exception as exc:
                # Gate returns code 1034 / AUTO_ORDER_NOT_FOUND when the
                # order already filled, expired, or was cancelled. That
                # means it no longer needs tracking, so treat it as
                # cleaned-up rather than spamming every tick.
                msg = str(exc)
                if "1034" in msg or "ORDER_NOT_FOUND" in msg.upper():
                    killed.append(order_id)
                    log.info("stale entry %s already gone — removing from pending", order_id)
                else:
                    log.warning("cancel stale failed %s: %s", order_id, exc)
        return killed
