"""Build the 10-minute LLM gate prompt from aggregated news.

News is bucketed by age (BREAKING ≤ NEWS_BREAKING_MIN, RECENT in
[breaking, NEWS_RECENT_MIN], STALE older). The system prompt instructs
Claude to treat each tier differently — only BREAKING news may solo-
trigger a directional or WAIT verdict, the older tiers are background
context. This implements semi-strong-form market efficiency: news
already in the chart should not move our risk decisions.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, List, Tuple

from config import KST, LLM_PROMPT_MAX_CHARS, NEWS_BREAKING_MIN, NEWS_RECENT_MIN
from news.base import NewsItem


_SYSTEM = """You are the final-gate risk officer for an ICT algorithmic
crypto-futures bot. The bot has already decided entries, stop-losses,
and take-profits. Your job is to decide — purely from news and macro
events — how the *next 10 minutes* should be traded.

CRITICAL — semi-strong-form market efficiency:
News already on the tape for more than ~15 minutes is mostly priced
in. Use the OLDER buckets only as background regime. They MUST NOT,
on their own, justify a directional or WAIT verdict. Only BREAKING
items (≤15 min) carry information the chart has not yet absorbed.

Verdicts (one per first line, exact token):
  PASS         both longs and shorts allowed at full size
  LONG_ONLY    shorts blocked; longs allowed at half size
  SHORT_ONLY   longs blocked; shorts allowed at half size
  WAIT         both blocked

Trigger conditions — apply ONLY to the BREAKING bucket plus the
upcoming-economic schedule. Default to PASS otherwise.

  WAIT  — high-impact USD macro release (CPI, NFP, FOMC, PCE) due in
          ≤15 min OR just dropped (≤5 min) with unclear direction.
          Confirmed major exploit / exchange outage / large regulatory
          action against a top-10 coin reported in the last 15 min.

  LONG_ONLY  — breaking news skews strongly bullish (e.g. surprise
          ETF approval, dovish Fed surprise that just printed,
          confirmed top-10 positive catalyst within last 15 min).

  SHORT_ONLY — breaking news skews strongly bearish (e.g. major
          exchange/protocol exploit just announced, hawkish surprise,
          geopolitics escalation in last 15 min).

  PASS  — everything else, INCLUDING:
          • only RECENT or STALE items present (already priced in)
          • breaking items but mixed/balanced sentiment
          • routine market commentary, generic risk-on/off chatter
          • single-source rumours not corroborated by other outlets

When in doubt, choose PASS. The bar for blocking trades is HIGH.

On the second line, a short reason (≤20 words). When triggering on
breaking news, name the item and its age (e.g. "FOMC surprise +25bp,
3m ago").
"""


def _age_minutes(item: NewsItem, now_utc: datetime) -> float:
    return (now_utc - item.published_at.astimezone(timezone.utc)).total_seconds() / 60.0


def _bucket(items: Iterable[NewsItem], now_utc: datetime) -> Tuple[List[NewsItem], List[NewsItem], List[NewsItem]]:
    breaking, recent, stale = [], [], []
    for it in items:
        if it.kind != "headline":
            continue
        age = _age_minutes(it, now_utc)
        if age <= NEWS_BREAKING_MIN:
            breaking.append(it)
        elif age <= NEWS_RECENT_MIN:
            recent.append(it)
        else:
            stale.append(it)
    breaking.sort(key=lambda x: x.published_at, reverse=True)
    recent.sort(key=lambda x: x.published_at, reverse=True)
    stale.sort(key=lambda x: x.published_at, reverse=True)
    return breaking, recent, stale


def _format_with_age(items: Iterable[NewsItem], now_utc: datetime, limit: int = 30) -> str:
    rows: List[str] = []
    for it in list(items)[:limit]:
        age = _age_minutes(it, now_utc)
        if age < 60:
            tag = f"{age:.0f}m"
        else:
            tag = f"{age/60:.1f}h"
        tickers = f" [{','.join(it.tickers)}]" if it.tickers else ""
        rows.append(f"- [{tag} ago]{tickers} {it.title}")
    return "\n".join(rows) if rows else "- (none)"


def _format_econ(items: Iterable[NewsItem]) -> str:
    rows: List[str] = []
    for it in items:
        rows.append(f"- {it.short()}")
    return "\n".join(rows) if rows else "- (none)"


def build_prompt(items: List[NewsItem], now: datetime | None = None) -> str:
    now = now or datetime.now(KST)
    now_utc = now.astimezone(timezone.utc)
    now_kst = now.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")

    breaking, recent, stale = _bucket(items, now_utc)

    econ = [i for i in items if i.kind == "econ"]
    econ_high = [i for i in econ if i.impact == "high"]
    econ_rest = [i for i in econ if i.impact != "high"]

    body = f"""{_SYSTEM}

Now: {now_kst}
Window to evaluate: next 10 minutes.
Age buckets: BREAKING ≤{NEWS_BREAKING_MIN}m, RECENT ≤{NEWS_RECENT_MIN}m, STALE older.

[HIGH-IMPACT ECONOMIC EVENTS — ±24h]
{_format_econ(econ_high)}

[OTHER ECONOMIC EVENTS]
{_format_econ(econ_rest[:20])}

[BREAKING HEADLINES — ≤{NEWS_BREAKING_MIN}m, NOT yet priced in]
{_format_with_age(breaking, now_utc, limit=30)}

[RECENT HEADLINES — {NEWS_BREAKING_MIN}-{NEWS_RECENT_MIN}m, partial absorption]
{_format_with_age(recent, now_utc, limit=20)}

[STALE HEADLINES — >{NEWS_RECENT_MIN}m, already priced in]
{_format_with_age(stale, now_utc, limit=15)}

Respond now. Line 1: PASS / LONG_ONLY / SHORT_ONLY / WAIT. Line 2: reason.
"""
    if len(body) > LLM_PROMPT_MAX_CHARS:
        body = body[: LLM_PROMPT_MAX_CHARS - 200] + "\n...[truncated]\n"
    return body
