"""Scan the universe and yield ICT signals.

Per cycle:
 * pull HTF / MTF / LTF OHLCV concurrently for each symbol
 * run the top-down pipeline
 * yield the symbols that produced a Signal
"""
from __future__ import annotations

import asyncio
from typing import List, Optional

from config import HTF_LOOKBACK, HTF_TIMEFRAME, LTF_LOOKBACK, LTF_TIMEFRAME, MTF_LOOKBACK, MTF_TIMEFRAME
from exchange.gateio import GateioFutures
from ict.autotune import record_signal
from ict.models import Signal
from ict.topdown import top_down
from utils.logger import get_logger

log = get_logger("scanner")


class Scanner:
    def __init__(self, ex: GateioFutures, max_concurrency: int = 6) -> None:
        self._ex = ex
        self._sem = asyncio.Semaphore(max_concurrency)

    async def scan_one(self, symbol: str) -> Optional[Signal]:
        async with self._sem:
            try:
                htf, mtf, ltf = await asyncio.gather(
                    self._ex.ohlcv(symbol, HTF_TIMEFRAME, HTF_LOOKBACK),
                    self._ex.ohlcv(symbol, MTF_TIMEFRAME, MTF_LOOKBACK),
                    self._ex.ohlcv(symbol, LTF_TIMEFRAME, LTF_LOOKBACK),
                )
            except Exception as exc:
                log.warning("ohlcv failed %s: %s", symbol, exc)
                return None
            try:
                return top_down(symbol, htf, mtf, ltf)
            except Exception as exc:
                log.exception("top_down %s crashed: %s", symbol, exc)
                return None

    async def scan(self, symbols: List[str]) -> List[Signal]:
        results = await asyncio.gather(*(self.scan_one(s) for s in symbols))
        signals = [s for s in results if s is not None]
        for sig in signals:
            record_signal(sig.symbol, sig.direction)
        if signals:
            log.info("scan produced %d signal(s) of %d scanned", len(signals), len(symbols))
        return signals
