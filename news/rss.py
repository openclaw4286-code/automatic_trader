"""Crypto news RSS fetchers (Coindesk, Cointelegraph)."""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import List

import httpx
from bs4 import BeautifulSoup

from news.base import NewsItem


class _RSS:
    url: str
    name: str

    async def fetch(self, lookback_min: int) -> List[NewsItem]:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(self.url, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            text = resp.text
        return self._parse(text, lookback_min)

    def _parse(self, text: str, lookback_min: int) -> List[NewsItem]:
        soup = BeautifulSoup(text, "xml")
        cutoff = datetime.now(timezone.utc).timestamp() - lookback_min * 60
        out: List[NewsItem] = []
        for item in soup.find_all("item"):
            ts = _parse_rss_date(_tag_text(item, "pubDate") or _tag_text(item, "updated"))
            if ts is None or ts.timestamp() < cutoff:
                continue
            out.append(
                NewsItem(
                    source=self.name,
                    kind="headline",
                    title=_tag_text(item, "title") or "",
                    url=_tag_text(item, "link") or "",
                    published_at=ts,
                    raw={"summary": _tag_text(item, "description") or ""},
                )
            )
        return out


def _tag_text(item, name: str) -> str | None:
    el = item.find(name)
    return el.get_text(strip=True) if el else None


def _parse_rss_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


class CoindeskRSS(_RSS):
    name = "rss_coindesk"
    url = "https://www.coindesk.com/arc/outboundfeeds/rss/"


class CointelegraphRSS(_RSS):
    name = "rss_cointelegraph"
    url = "https://cointelegraph.com/rss"
