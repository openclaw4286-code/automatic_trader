"""Universe selector: CoinGecko top-N market cap ∩ Gate.io USDT-perp."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List

import httpx

from config import (
    COINGECKO_URL,
    EXCLUDE_SYMBOLS,
    UNIVERSE_REFRESH_MIN,
    UNIVERSE_SIZE,
)
from exchange.gateio import GateioFutures
from utils.logger import get_logger

log = get_logger("universe")


@dataclass
class UniverseEntry:
    base: str          # e.g. "BTC"
    symbol: str        # unified ccxt symbol, e.g. "BTC/USDT:USDT"
    rank: int
    market_cap: float


class Universe:
    def __init__(self, ex: GateioFutures) -> None:
        self._ex = ex
        self._entries: List[UniverseEntry] = []
        self._fetched_at: float = 0.0

    async def refresh(self, force: bool = False) -> List[UniverseEntry]:
        if not force and self._entries:
            age_min = (time.time() - self._fetched_at) / 60
            if age_min < UNIVERSE_REFRESH_MIN:
                return self._entries

        await self._ex.load()

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                COINGECKO_URL,
                params={
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": 100,
                    "page": 1,
                    "sparkline": "false",
                },
            )
            resp.raise_for_status()
            rows = resp.json()

        entries: List[UniverseEntry] = []
        for row in rows:
            base = str(row.get("symbol", "")).upper()
            if not base or base in EXCLUDE_SYMBOLS:
                continue
            symbol = self._ex.symbol_for(base)
            if not symbol:
                continue
            entries.append(
                UniverseEntry(
                    base=base,
                    symbol=symbol,
                    rank=int(row.get("market_cap_rank") or 0),
                    market_cap=float(row.get("market_cap") or 0.0),
                )
            )
            if len(entries) >= UNIVERSE_SIZE:
                break

        self._entries = entries
        self._fetched_at = time.time()
        log.info("universe refreshed: %d symbols (top%d ∩ gate)", len(entries), UNIVERSE_SIZE)
        return entries

    def symbols(self) -> List[str]:
        return [e.symbol for e in self._entries]
