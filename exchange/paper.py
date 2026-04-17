"""In-memory paper-trading engine used when DRY_RUN=true.

Scope:
 * entry orders (non-reduce-only) fill immediately at their limit price
 * SL = reduce-only with a triggerPrice / stopPrice param (stop-market)
 * TP = reduce-only without a stop price (limit at price)
 * tick() polls the REAL Gate.io ticker for each tracked symbol and
   virtually fills any SL or TP whose trigger has been crossed,
   shrinking or closing the underlying paper position accordingly
 * positions() / open_orders() / cancel_order() serve the paper state

Everything else (load_markets, ohlcv, ticker, equity) still hits the
real Gate.io read-only API so ICT analysis and sizing use live data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from utils.logger import get_logger

if TYPE_CHECKING:
    from exchange.gateio import GateioFutures

log = get_logger("paper")


@dataclass
class _Position:
    symbol: str
    side: str                  # "long" | "short"
    contracts: float
    entry_price: float
    mark_price: float
    opened_at: float = 0.0

    def as_ccxt(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "contracts": self.contracts,
            "entryPrice": self.entry_price,
            "markPrice": self.mark_price,
        }


@dataclass
class _Order:
    id: str
    symbol: str
    side: str                  # "buy" | "sell"
    amount: float
    price: Optional[float] = None
    stop_price: Optional[float] = None
    reduce_only: bool = False
    is_market: bool = False
    status: str = "open"

    def as_ccxt(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "symbol": self.symbol,
            "side": self.side,
            "amount": self.amount,
            "price": self.price,
            "stopPrice": self.stop_price,
            "reduceOnly": self.reduce_only,
            "status": self.status,
        }


class DryBroker:
    def __init__(self, ex: "GateioFutures") -> None:
        self._ex = ex
        self._orders: Dict[str, _Order] = {}
        self._positions: Dict[str, _Position] = {}
        self._next_id = 0
        self.realized_pnl: float = 0.0

    # ------------------------------------------------------------------ id
    def _mint_id(self) -> str:
        self._next_id += 1
        return f"paper-{self._next_id}"

    # -------------------------------------------------------- order routing
    async def create_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: Optional[float],
        params: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        p = params or {}
        reduce_only = bool(p.get("reduceOnly"))
        stop_price = p.get("stopPrice") or p.get("triggerPrice")
        is_market = price is None or p.get("type") == "market"
        oid = self._mint_id()

        if not reduce_only:
            # --------- entry: immediate fill at the limit price ----------
            fill_px = float(price) if price is not None else self._positions.get(symbol, _Position(symbol, "long", 0, 0, 0)).entry_price
            direction = "long" if side == "buy" else "short"
            self._positions[symbol] = _Position(
                symbol=symbol,
                side=direction,
                contracts=float(amount),
                entry_price=float(fill_px),
                mark_price=float(fill_px),
            )
            log.info("[paper FILL entry] %s %s qty=%.6g @ %.6g", side, symbol, amount, fill_px)
            return {
                "id": oid,
                "symbol": symbol,
                "side": side,
                "amount": amount,
                "price": fill_px,
                "status": "closed",
            }

        # --------- reduce-only: SL or TP; also covers emergency market close
        if is_market and stop_price is None:
            # immediate market reduce-only (emergency_close / partial_close)
            return await self._execute_reduce_market(symbol, side, float(amount), oid)

        order = _Order(
            id=oid,
            symbol=symbol,
            side=side,
            amount=float(amount),
            price=float(price) if price is not None else None,
            stop_price=float(stop_price) if stop_price is not None else None,
            reduce_only=True,
            is_market=is_market,
        )
        self._orders[oid] = order
        kind = "SL" if order.stop_price is not None else "TP"
        log.info(
            "[paper ORDER %s] %s %s qty=%.6g trigger=%s price=%s id=%s",
            kind, side, symbol, amount, order.stop_price, order.price, oid,
        )
        return order.as_ccxt()

    async def cancel_order(self, order_id: str, symbol: Optional[str] = None) -> Dict[str, Any]:
        o = self._orders.pop(order_id, None)
        log.info("[paper CANCEL] %s %s", order_id, "ok" if o else "missing")
        return {"id": order_id, "symbol": symbol, "status": "canceled"}

    async def open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        return [o.as_ccxt() for o in self._orders.values() if symbol is None or o.symbol == symbol]

    async def positions(self) -> List[Dict[str, Any]]:
        return [p.as_ccxt() for p in self._positions.values() if p.contracts > 0]

    # ------------------------------------------------------------ virtual fills
    async def tick(self) -> None:
        """Pull live prices and fire any trigger orders that have crossed."""
        for sym in list(self._positions):
            pos = self._positions[sym]
            try:
                tkr = await self._ex._fetch_ticker_real(sym)       # bypass paper
            except Exception as exc:
                log.warning("paper ticker %s failed: %s", sym, exc)
                continue
            mark = float(tkr.get("last") or tkr.get("close") or pos.entry_price)
            pos.mark_price = mark

            # SL first — more conservative
            for o in list(self._orders.values()):
                if o.symbol != sym or o.stop_price is None:
                    continue
                if _triggered_sl(pos.side, mark, o.stop_price):
                    self._fill_at(o, pos, o.stop_price, kind="SL")
                    if pos.contracts <= 0:
                        break

            if sym not in self._positions:
                continue

            for o in list(self._orders.values()):
                if o.symbol != sym or o.stop_price is not None or not o.reduce_only:
                    continue
                if o.price is None:
                    continue
                if _triggered_tp(pos.side, mark, o.price):
                    self._fill_at(o, pos, o.price, kind="TP")
                    if pos.contracts <= 0:
                        break

    # ------------------------------------------------------------ helpers
    async def _execute_reduce_market(self, symbol: str, side: str, amount: float, oid: str) -> Dict[str, Any]:
        pos = self._positions.get(symbol)
        if pos is None:
            log.warning("[paper] reduce-only market on %s with no position", symbol)
            return {"id": oid, "symbol": symbol, "status": "rejected"}
        fill_px = pos.mark_price or pos.entry_price
        # synthesize an order just to pass through _fill_at accounting
        fake = _Order(id=oid, symbol=symbol, side=side, amount=amount, reduce_only=True, is_market=True)
        self._fill_at(fake, pos, fill_px, kind="MKT")
        return {"id": oid, "symbol": symbol, "status": "closed", "price": fill_px, "amount": amount}

    def _fill_at(self, order: _Order, pos: _Position, price: float, *, kind: str) -> None:
        close_qty = min(order.amount, pos.contracts)
        pnl_per_unit = (price - pos.entry_price) if pos.side == "long" else (pos.entry_price - price)
        pnl = pnl_per_unit * close_qty
        self.realized_pnl += pnl
        pos.contracts -= close_qty
        log.info(
            "[paper FILL %s] %s qty=%.6g @ %.6g  pnl=%+.4f  remaining=%.6g",
            kind, pos.symbol, close_qty, price, pnl, pos.contracts,
        )
        # remove / shrink the order
        if order.amount <= close_qty:
            self._orders.pop(order.id, None)
        else:
            order.amount -= close_qty
        # if position flat, wipe any leftover orders for this symbol
        if pos.contracts <= 0:
            self._positions.pop(pos.symbol, None)
            for oid in [oid for oid, o in self._orders.items() if o.symbol == pos.symbol]:
                self._orders.pop(oid, None)
            log.info("[paper CLOSED] %s realized pnl=%+.4f", pos.symbol, self.realized_pnl)


def _triggered_sl(side: str, mark: float, stop: float) -> bool:
    return mark <= stop if side == "long" else mark >= stop


def _triggered_tp(side: str, mark: float, target: float) -> bool:
    return mark >= target if side == "long" else mark <= target
