"""Tries news sources in the configured order and caches what works.

Design:
 * iterate NEWS_SOURCES until at least one returns items successfully
 * on each cycle, successful sources are kept; failing ones drop to the
   back of the queue so we do not hammer a broken endpoint
 * dedupes by (source, title) and sorts by time desc
"""
from __future__ import annotations

import asyncio
from typing import Dict, List

from config import NEWS_LOOKBACK_MIN, NEWS_SOURCES
from news.base import Fetcher, NewsItem
from news.cryptopanic import CryptoPanic
from news.econ_calendar import ForexFactory, InvestingCalendar
from news.rss import CoindeskRSS, CointelegraphRSS
from utils.logger import get_logger

log = get_logger("news")


_REGISTRY: Dict[str, type[Fetcher]] = {
    "cryptopanic": CryptoPanic,
    "rss_coindesk": CoindeskRSS,
    "rss_cointelegraph": CointelegraphRSS,
    "forexfactory": ForexFactory,
    "investing_economic": InvestingCalendar,
}


class NewsAggregator:
    def __init__(self, sources: List[str] | None = None) -> None:
        names = sources or NEWS_SOURCES
        self._order: List[str] = [n for n in names if n in _REGISTRY]
        self._fetchers: Dict[str, Fetcher] = {n: _REGISTRY[n]() for n in self._order}
        self._health: Dict[str, bool] = {n: True for n in self._order}

    async def fetch(self, lookback_min: int | None = None) -> List[NewsItem]:
        lookback = lookback_min or NEWS_LOOKBACK_MIN
        order = sorted(self._order, key=lambda n: (not self._health.get(n, True)))

        tasks = {n: asyncio.create_task(self._safe(n, lookback)) for n in order}
        results: List[NewsItem] = []
        for name, task in tasks.items():
            items = await task
            if items:
                self._health[name] = True
                results.extend(items)
            else:
                self._health[name] = False

        return _dedupe_sort(results)

    async def _safe(self, name: str, lookback: int) -> List[NewsItem]:
        try:
            return await self._fetchers[name].fetch(lookback)
        except Exception as exc:
            log.warning("news source %s failed: %s", name, exc)
            return []

    def health(self) -> Dict[str, bool]:
        return dict(self._health)


def _dedupe_sort(items: List[NewsItem]) -> List[NewsItem]:
    # newest-first; when titles collide (same source) keep the newer one
    items = sorted(items, key=lambda x: x.published_at, reverse=True)
    seen: set[tuple[str, str]] = set()
    out: List[NewsItem] = []
    for it in items:
        key = (it.source, it.title.strip().lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out
