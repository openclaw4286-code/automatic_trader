"""Async Gate.io USDT-perpetual wrapper over ccxt.

Responsibilities:
 * fetch OHLCV on demand
 * balance / positions / markets
 * place, cancel, and query orders
 * convert a CoinGecko base ticker (e.g. "BTC") into the unified ccxt symbol

When DRY_RUN=true, write-ish calls (create_order, cancel_order) and
the state they produce (positions, open_orders) are routed to an
in-memory DryBroker so the full entry → SL/TP → partial-TP → trail →
close lifecycle can be observed with zero real order flow. Read-only
market data still comes from real Gate.io.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import ccxt.async_support as ccxt
import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential_jitter

from config import (
    DRY_RUN,
    EXCHANGE_ID,
    GATE_API_KEY,
    GATE_API_SECRET,
    MARKET_TYPE,
    SETTLE_CCY,
)
from utils.logger import get_logger

log = get_logger("exchange")

_RETRY = dict(
    stop=stop_after_attempt(4),
    wait=wait_exponential_jitter(initial=1, max=16),
    reraise=True,
)


class GateioFutures:
    """Thin async wrapper; one instance per runner."""

    def __init__(self) -> None:
        self._ex: ccxt.gateio = ccxt.gateio(
            {
                "apiKey": GATE_API_KEY,
                "secret": GATE_API_SECRET,
                "enableRateLimit": True,
                "options": {"defaultType": MARKET_TYPE, "defaultSettle": SETTLE_CCY},
            }
        )
        self._markets: Dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._paper = None           # lazily built when DRY_RUN
        if DRY_RUN:
            from exchange.paper import DryBroker
            self._paper = DryBroker(self)

    # ------------------------------------------------------------------ setup
    async def load(self) -> None:
        async with self._lock:
            if not self._markets:
                self._markets = await self._ex.load_markets()
                log.info("loaded %d %s markets", len(self._markets), EXCHANGE_ID)

    async def close(self) -> None:
        await self._ex.close()

    # --------------------------------------------------------------- helpers
    def symbol_for(self, base: str) -> Optional[str]:
        """Return the unified ccxt symbol for `base/USDT` perp, or None."""
        want = f"{base.upper()}/{SETTLE_CCY}:{SETTLE_CCY}"
        return want if want in self._markets else None

    def list_swap_symbols(self) -> List[str]:
        return [
            s
            for s, m in self._markets.items()
            if m.get("swap") and m.get("settle") == SETTLE_CCY and m.get("active")
        ]

    # ----------------------------------------------------------------- data
    @retry(**_RETRY)
    async def ohlcv(self, symbol: str, timeframe: str, limit: int = 300) -> pd.DataFrame:
        rows = await self._ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        return df

    @retry(**_RETRY)
    async def ticker(self, symbol: str) -> Dict[str, Any]:
        return await self._ex.fetch_ticker(symbol)

    async def _fetch_ticker_real(self, symbol: str) -> Dict[str, Any]:
        """Bypass the paper broker — used by DryBroker.tick to poll mark prices."""
        return await self._ex.fetch_ticker(symbol)

    # -------------------------------------------------------------- account
    @retry(**_RETRY)
    async def equity_usdt(self) -> float:
        bal = await self._ex.fetch_balance({"type": MARKET_TYPE})
        total = bal.get("total", {}).get(SETTLE_CCY) or 0.0
        return float(total)

    async def positions(self) -> List[Dict[str, Any]]:
        if self._paper is not None:
            return await self._paper.positions()
        return await self._positions_real()

    @retry(**_RETRY)
    async def _positions_real(self) -> List[Dict[str, Any]]:
        poss = await self._ex.fetch_positions()
        return [p for p in poss if float(p.get("contracts") or 0) > 0]

    # ---------------------------------------------------------------- trade
    async def set_leverage(self, symbol: str, leverage: int) -> None:
        if DRY_RUN:
            log.info("[dry] set_leverage %s x%d", symbol, leverage)
            return
        try:
            await self._ex.set_leverage(leverage, symbol)
        except Exception as exc:  # Gate returns an error if already set
            log.warning("set_leverage %s x%d: %s", symbol, leverage, exc)

    async def create_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: Optional[float] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if self._paper is not None:
            return await self._paper.create_order(symbol, side, amount, price, params)
        return await self._create_order_real(symbol, side, amount, price, params)

    @retry(**_RETRY)
    async def _create_order_real(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: Optional[float],
        params: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        order_type = "limit" if price is not None else "market"
        return await self._ex.create_order(symbol, order_type, side, amount, price, params or {})

    async def cancel_order(self, order_id: str, symbol: str) -> Dict[str, Any]:
        if self._paper is not None:
            return await self._paper.cancel_order(order_id, symbol)
        return await self._cancel_order_real(order_id, symbol)

    @retry(**_RETRY)
    async def _cancel_order_real(self, order_id: str, symbol: str) -> Dict[str, Any]:
        return await self._ex.cancel_order(order_id, symbol)

    async def open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        if self._paper is not None:
            return await self._paper.open_orders(symbol)
        return await self._open_orders_real(symbol)

    @retry(**_RETRY)
    async def _open_orders_real(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        return await self._ex.fetch_open_orders(symbol)

    # ---------------------------------------------------------- paper tick
    async def paper_tick(self) -> None:
        """No-op in live mode; in DRY_RUN drives virtual SL/TP fills."""
        if self._paper is not None:
            await self._paper.tick()
