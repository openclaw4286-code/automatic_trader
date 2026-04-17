"""Turn a PositionPlan into concrete exchange orders.

Strategy:
 * set leverage for the symbol (isolated margin)
 * place a limit *entry* at the plan price — TTL cancellation handled by
   the monitor loop
 * place a reduce-only stop-market as SL (triggerPrice = plan.sl)
 * place a reduce-only limit as TP (price = plan.tp)

Gate.io-specific params are isolated here so if the ccxt surface
changes we only touch this file. In DRY_RUN every call just logs.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from config import ORDER_TTL_SEC
from exchange.gateio import GateioFutures
from risk.sizing import PositionPlan
from utils.logger import get_logger

log = get_logger("executor")


@dataclass
class PlacedOrders:
    entry_id: str
    sl_id: str
    tp_id: str
    symbol: str
    plan: PositionPlan


class Executor:
    def __init__(self, ex: GateioFutures) -> None:
        self._ex = ex

    async def place(self, plan: PositionPlan) -> PlacedOrders:
        sym = plan.symbol
        await self._ex.set_leverage(sym, plan.leverage)

        # --- entry (limit) ----
        entry = await self._ex.create_order(
            symbol=sym,
            side=plan.side,
            amount=plan.qty_contracts,
            price=plan.entry,
            params={"timeInForce": "GTC"},
        )

        close_side = "sell" if plan.side == "buy" else "buy"

        # --- stop loss (reduce-only stop market) ----
        sl = await self._ex.create_order(
            symbol=sym,
            side=close_side,
            amount=plan.qty_contracts,
            price=None,
            params={
                "reduceOnly": True,
                "stopPrice": plan.sl,
                "triggerPrice": plan.sl,
                "trigger": "last",
            },
        )

        # --- take profit (reduce-only limit) ----
        tp = await self._ex.create_order(
            symbol=sym,
            side=close_side,
            amount=plan.qty_contracts,
            price=plan.tp,
            params={"reduceOnly": True},
        )

        log.info("placed: %s", plan.as_log())
        return PlacedOrders(
            entry_id=str(entry.get("id")),
            sl_id=str(sl.get("id")),
            tp_id=str(tp.get("id")),
            symbol=sym,
            plan=plan,
        )

    async def cancel_stale(self, pending: Dict[str, Dict[str, Any]], now_ts: float) -> List[str]:
        """Cancel any entry order older than ORDER_TTL_SEC that has not filled."""
        killed: List[str] = []
        for order_id, meta in list(pending.items()):
            if now_ts - meta["placed_at"] < ORDER_TTL_SEC:
                continue
            try:
                await self._ex.cancel_order(order_id, meta["symbol"])
                killed.append(order_id)
                log.info("cancelled stale entry %s (%s)", order_id, meta["symbol"])
            except Exception as exc:
                log.warning("cancel stale failed %s: %s", order_id, exc)
        return killed
