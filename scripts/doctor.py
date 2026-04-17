"""Preflight checks before running the bot.

Runs quickly and reports pass/fail for every externality the main
runner depends on:

 1. .env present and required keys set (or DRY_RUN=true)
 2. Python deps importable
 3. Claude CLI binary on PATH
 4. Gate.io reachable + USDT futures markets load
 5. CoinGecko reachable
 6. Session window status (KST)
 7. One Claude CLI round-trip (sanity "PASS" probe) — optional, skipped
    if --skip-claude is passed

Usage:
   python scripts/doctor.py
   python scripts/doctor.py --skip-claude
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import os
import shutil
import sys
from datetime import datetime

from rich.console import Console

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

console = Console()
OK = "[green]✓[/green]"
BAD = "[red]✗[/red]"
WARN = "[yellow]![/yellow]"


def check_env() -> bool:
    from config import DRY_RUN, GATE_API_KEY, GATE_API_SECRET

    if DRY_RUN:
        console.print(f"{OK} DRY_RUN=true — trading calls will be simulated")
    else:
        if not GATE_API_KEY or not GATE_API_SECRET:
            console.print(f"{BAD} DRY_RUN=false but GATE_API_KEY/SECRET missing in .env")
            return False
        console.print(f"{OK} live mode: Gate.io keys present")
    return True


def check_deps() -> bool:
    missing = []
    for mod in ("ccxt", "pandas", "numpy", "httpx", "bs4", "lxml", "rich", "tenacity", "dotenv"):
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        console.print(f"{BAD} missing deps: {', '.join(missing)} — run scripts/setup.sh")
        return False
    console.print(f"{OK} python dependencies importable")
    return True


def check_claude_bin() -> bool:
    from config import CLAUDE_CLI

    path = shutil.which(CLAUDE_CLI)
    if not path:
        console.print(f"{BAD} claude CLI not found (looked for '{CLAUDE_CLI}')")
        return False
    console.print(f"{OK} claude CLI found at {path}")
    return True


async def check_gate() -> bool:
    from exchange.gateio import GateioFutures

    ex = GateioFutures()
    try:
        await ex.load()
        swaps = ex.list_swap_symbols()
        console.print(f"{OK} Gate.io markets loaded ({len(swaps)} USDT-perp)")
        return True
    except Exception as exc:
        console.print(f"{BAD} Gate.io load failed: {exc}")
        return False
    finally:
        await ex.close()


async def check_coingecko() -> bool:
    import httpx

    from config import COINGECKO_URL

    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(COINGECKO_URL, params={"vs_currency": "usd", "per_page": 5, "page": 1})
            r.raise_for_status()
            top = [x["symbol"].upper() for x in r.json()[:3]]
        console.print(f"{OK} CoinGecko reachable (top-3: {','.join(top)})")
        return True
    except Exception as exc:
        console.print(f"{BAD} CoinGecko failed: {exc}")
        return False


def check_session() -> bool:
    from utils.session import current_window, in_session

    if in_session():
        w = current_window()
        console.print(f"{OK} currently in session window {w[0]}~{w[1]} KST")
    else:
        now = datetime.now().astimezone()
        console.print(f"{WARN} outside all session windows (now={now.strftime('%H:%M %Z')}) — scanner will idle")
    return True


async def check_claude_probe() -> bool:
    from llm.claude_cli import ClaudeCLI, parse_verdict

    cli = ClaudeCLI()
    prompt = (
        "You are a test probe. Reply exactly with PASS on line 1 and 'doctor ok' on line 2."
    )
    try:
        reply = await cli.ask(prompt)
    except Exception as exc:
        console.print(f"{BAD} claude CLI call failed: {exc}")
        return False
    status, _ = parse_verdict(reply)
    if status == "PASS":
        console.print(f"{OK} claude CLI round-trip PASS")
        return True
    console.print(f"{WARN} claude CLI responded but not PASS (got {status}) — check Max plan / model access")
    return False


async def main(skip_claude: bool) -> int:
    console.rule("[bold cyan]ICT auto-trader — preflight doctor")
    results = [
        check_env(),
        check_deps(),
        check_claude_bin(),
        await check_gate(),
        await check_coingecko(),
        check_session(),
    ]
    if not skip_claude:
        results.append(await check_claude_probe())
    console.rule()
    if all(results):
        console.print("[bold green]all checks passed — safe to run `python -m runner.main`[/bold green]")
        return 0
    console.print("[bold red]some checks failed — fix the items above before running[/bold red]")
    return 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-claude", action="store_true", help="skip the claude round-trip probe")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.skip_claude)))
