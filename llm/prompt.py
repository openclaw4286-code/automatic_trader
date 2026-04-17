"""Build the 10-minute LLM gate prompt from aggregated news."""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, List

from config import KST, LLM_PROMPT_MAX_CHARS
from news.base import NewsItem


_SYSTEM = """You are the final-gate risk officer for an ICT algorithmic
crypto-futures bot. The bot has already decided entries, stop-losses,
and take-profits. Your job is to decide whether the *next 10 minutes*
is a safe window to place those orders, purely from news and economic
events. Answer on a single first line with exactly one token:
PASS or WAIT. On the second line, a short reason (<= 20 words).

Rules:
- WAIT if a high-impact USD macro release (CPI, NFP, FOMC, PCE) is due
  within the next 15 minutes or has just dropped (last 5 minutes).
- WAIT on confirmed major exploit / exchange outage / large regulatory
  action news against a top-10 coin.
- PASS otherwise, even if headlines are noisy.
"""


def _format_items(items: Iterable[NewsItem]) -> str:
    lines: List[str] = []
    for it in items:
        lines.append(f"- {it.short()}")
    return "\n".join(lines) if lines else "- (no recent items)"


def build_prompt(items: List[NewsItem], now: datetime | None = None) -> str:
    now = now or datetime.now(KST)
    now_kst = now.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")

    headlines = [i for i in items if i.kind == "headline"]
    econ = [i for i in items if i.kind == "econ"]
    econ_high = [i for i in econ if i.impact == "high"]
    econ_rest = [i for i in econ if i.impact != "high"]

    body = f"""{_SYSTEM}

Now: {now_kst}
Window to evaluate: next 10 minutes.

[HIGH-IMPACT ECONOMIC EVENTS — ±24h]
{_format_items(econ_high)}

[OTHER ECONOMIC EVENTS]
{_format_items(econ_rest[:20])}

[HEADLINES — most recent first]
{_format_items(headlines[:40])}

Respond now. Line 1: PASS or WAIT. Line 2: reason.
"""
    if len(body) > LLM_PROMPT_MAX_CHARS:
        body = body[: LLM_PROMPT_MAX_CHARS - 200] + "\n...[truncated]\n"
    return body
