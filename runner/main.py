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
import os
import signal as os_signal

from config import (
    CLEAN_START,
    DRY_RUN,
    LLM_GATE_INTERVAL_SEC,
    POSITION_POLL_SEC,
    SCAN_INTERVAL_SEC,
)
from datetime import datetime

from exchange.gateio import GateioFutures
from exchange.universe import Universe
from ict.autotune import apply_state, tune_once, current_levels
from llm.gate import LLMGate
from runner.manager import TradeManager
from runner.scanner import Scanner
from utils.logger import get_logger
from utils.session import crossed_weekend_boundary, in_session

log = get_logger("main")


async def flatten_startup_state(ex: GateioFutures) -> None:
    """Cancel every open order and market-close every live position so
    the bot never inherits dangling SL/TP or positions from a previous
    (possibly buggy) run. No-op in DRY_RUN because the paper broker
    starts fresh in-process anyway."""
    if DRY_RUN:
        return

    try:
        open_orders = await ex.open_orders()
    except Exception as exc:
        log.warning("startup flatten: open_orders() failed: %s", exc)
        open_orders = []

    cancelled = 0
    for o in open_orders:
        oid = str(o.get("id"))
        sym = o.get("symbol")
        try:
            await ex.cancel_order(oid, sym)
            cancelled += 1
        except Exception as exc:
            log.warning("startup flatten: cancel %s on %s failed: %s", oid, sym, exc)
    if cancelled:
        log.info("startup flatten: cancelled %d open order(s)", cancelled)

    try:
        positions = await ex.positions()
    except Exception as exc:
        log.warning("startup flatten: positions() failed: %s", exc)
        positions = []

    closed = 0
    for p in positions:
        sym = p.get("symbol")
        qty = abs(float(p.get("contracts") or 0))
        if qty <= 0:
            continue
        side_raw = (p.get("side") or "").lower()
        direction = "long" if side_raw in ("long", "buy") else "short"
        close_side = "sell" if direction == "long" else "buy"
        try:
            await ex.create_order(sym, close_side, qty, None, {"reduceOnly": True})
            closed += 1
            log.info("startup flatten: market-closed %s %s qty=%.6g", direction, sym, qty)
        except Exception as exc:
            log.warning("startup flatten: close %s failed: %s", sym, exc)
    if closed:
        log.info("startup flatten: closed %d live position(s)", closed)
    if not cancelled and not closed:
        log.info("startup flatten: nothing to clean")


async def gate_loop(gate: LLMGate) -> None:
    while True:
        try:
            await gate.evaluate_once()
        except Exception as exc:
            log.exception("gate_loop: %s", exc)
        await asyncio.sleep(LLM_GATE_INTERVAL_SEC)


async def scan_loop(ex: GateioFutures, universe: Universe, scanner: Scanner, manager: TradeManager) -> None:
    last_tune = datetime.now().astimezone()
    while True:
        try:
            if in_session():
                await universe.refresh()
                signals = await scanner.scan(universe.symbols())
                if signals:
                    await manager.process(signals)
            else:
                log.debug("out of session — scan skipped")

            # hourly autotune check (rate-limited internally to ≥55 min)
            now = datetime.now().astimezone()
            if (now - last_tune).total_seconds() >= 3600:
                tune_once()
                last_tune = now
        except Exception as exc:
            log.exception("scan_loop: %s", exc)
        await asyncio.sleep(SCAN_INTERVAL_SEC)


async def monitor_loop(ex: GateioFutures, manager: TradeManager) -> None:
    last_tick = datetime.now().astimezone()
    while True:
        try:
            await ex.paper_tick()                   # DRY_RUN: virtual SL/TP fills
            await manager.position_manager.tick()   # protection + partial TP + trail
            await manager.cleanup_stale()

            now = datetime.now().astimezone()
            if crossed_weekend_boundary(last_tick, now):
                log.warning("weekend boundary crossed — flattening all state")
                await flatten_startup_state(ex)
            last_tick = now
        except Exception as exc:
            log.exception("monitor_loop: %s", exc)
        await asyncio.sleep(POSITION_POLL_SEC)


async def amain() -> None:
    if not DRY_RUN and os.getenv("LIVE_CONFIRMED", "").lower() not in ("1", "true", "yes"):
        msg = (
            "DRY_RUN=false detected but LIVE_CONFIRMED env var is not set.\n"
            "This is a safety guard: real orders will be sent to Gate.io.\n"
            "To proceed: LIVE_CONFIRMED=true python -m runner.main\n"
            "Or better, use ./scripts/go_live.sh which walks through the "
            "safe-start protocol."
        )
        print(msg)
        log.error("live-mode startup blocked — see stdout")
        return
    log.info("starting ICT auto-trader (DRY_RUN=%s)", DRY_RUN)
    ex = GateioFutures()
    await ex.load()

    if CLEAN_START:
        await flatten_startup_state(ex)

    # Apply persisted ladder positions — restarts must NOT trigger a
    # tune step (that bug caused runaway tightening when the user
    # restarted the bot a few times while count was above target).
    apply_state()
    log.info("autotune levels on startup: %s", current_levels())

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
