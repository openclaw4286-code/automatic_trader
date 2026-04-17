"""Async entry point. Three concurrent loops:

 * gate_loop     — every LLM_GATE_INTERVAL_SEC, refresh PASS/WAIT
 * scan_loop     — every SCAN_INTERVAL_SEC (in-session only), run ICT
                   top-down across the universe and dispatch candidates
                   through the TradeManager
 * monitor_loop  — every POSITION_POLL_SEC, cancel stale entry orders
                   and print account / position summary
"""
from __future__ import annotations

import asyncio
import signal as os_signal

from config import (
    DRY_RUN,
    LLM_GATE_INTERVAL_SEC,
    POSITION_POLL_SEC,
    SCAN_INTERVAL_SEC,
)
from exchange.gateio import GateioFutures
from exchange.universe import Universe
from llm.gate import LLMGate
from runner.manager import TradeManager
from runner.scanner import Scanner
from utils.logger import get_logger
from utils.session import in_session

log = get_logger("main")


async def gate_loop(gate: LLMGate) -> None:
    while True:
        try:
            await gate.evaluate_once()
        except Exception as exc:
            log.exception("gate_loop: %s", exc)
        await asyncio.sleep(LLM_GATE_INTERVAL_SEC)


async def scan_loop(ex: GateioFutures, universe: Universe, scanner: Scanner, manager: TradeManager) -> None:
    while True:
        try:
            if in_session():
                await universe.refresh()
                signals = await scanner.scan(universe.symbols())
                if signals:
                    await manager.process(signals)
            else:
                log.debug("out of session — scan skipped")
        except Exception as exc:
            log.exception("scan_loop: %s", exc)
        await asyncio.sleep(SCAN_INTERVAL_SEC)


async def monitor_loop(ex: GateioFutures, manager: TradeManager) -> None:
    while True:
        try:
            await ex.paper_tick()                   # DRY_RUN: virtual SL/TP fills
            await manager.position_manager.tick()   # protection + partial TP + trail
            await manager.cleanup_stale()
            positions = await ex.positions()
            equity = await ex.equity_usdt()
            log.info(
                "equity=%.2f USDT  live=%d  tracked=%d",
                equity,
                len(positions),
                len(manager.position_manager.tracked()),
            )
        except Exception as exc:
            log.exception("monitor_loop: %s", exc)
        await asyncio.sleep(POSITION_POLL_SEC)


async def amain() -> None:
    log.info("starting ICT auto-trader (DRY_RUN=%s)", DRY_RUN)
    ex = GateioFutures()
    await ex.load()
    universe = Universe(ex)
    await universe.refresh(force=True)
    scanner = Scanner(ex)
    gate = LLMGate()
    manager = TradeManager(ex, gate)

    stop = asyncio.Event()

    def _graceful(*_):
        log.info("shutdown requested")
        stop.set()

    for sig in (os_signal.SIGINT, os_signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, _graceful)
        except NotImplementedError:
            pass

    tasks = [
        asyncio.create_task(gate_loop(gate)),
        asyncio.create_task(scan_loop(ex, universe, scanner, manager)),
        asyncio.create_task(monitor_loop(ex, manager)),
    ]
    try:
        await stop.wait()
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await ex.close()
        log.info("stopped")


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
