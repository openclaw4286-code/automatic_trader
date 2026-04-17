"""Crypto news RSS fetchers.

Lesson learned from the field: Coindesk's arc/outboundfeeds endpoint
frequently 403s requests without a real-browser User-Agent, and some
CDN edges time out entirely. Each fetcher now:

 * sends a realistic User-Agent (otherwise many edges return 403/429),
 * follows redirects,
 * logs HTTP status and item count so aggregate diagnostics are useful,
 * accepts either RSS or Atom <entry> shapes,
 * accepts <link href="..."> as well as <link>text</link>.
"""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import List

import httpx
from bs4 import BeautifulSoup

from news.base import NewsItem
from utils.logger import get_logger

log = get_logger("news_rss")


_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"
)
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.5",
    "Accept-Language": "en-US,en;q=0.9",
}


class _RSS:
    url: str
    name: str

    async def fetch(self, lookback_min: int) -> List[NewsItem]:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers=_HEADERS) as client:
            try:
                resp = await client.get(self.url)
            except Exception as exc:
                log.warning("%s GET failed: %s", self.name, exc)
                raise
            if resp.status_code != 200:
                log.warning("%s HTTP %s (%d bytes)", self.name, resp.status_code, len(resp.content))
                resp.raise_for_status()
            text = resp.text
        items = self._parse(text, lookback_min)
        log.info("%s parsed %d item(s)", self.name, len(items))
        return items

    def _parse(self, text: str, lookback_min: int) -> List[NewsItem]:
        soup = BeautifulSoup(text, "xml")
        cutoff = datetime.now(timezone.utc).timestamp() - lookback_min * 60
        out: List[NewsItem] = []
        # RSS 2.0 uses <item>, Atom uses <entry>
        nodes = soup.find_all("item") + soup.find_all("entry")
        for node in nodes:
            ts = _entry_time(node)
            if ts is None or ts.timestamp() < cutoff:
                continue
            out.append(
                NewsItem(
                    source=self.name,
                    kind="headline",
                    title=_tag_text(node, "title") or "",
                    url=_link_url(node),
                    published_at=ts,
                    raw={"summary": _tag_text(node, "description") or _tag_text(node, "summary") or ""},
                )
            )
        return out


def _tag_text(node, name: str) -> str | None:
    el = node.find(name)
    return el.get_text(strip=True) if el else None


def _link_url(node) -> str:
    el = node.find("link")
    if not el:
        return ""
    href = el.get("href")
    if href:
        return href
    return el.get_text(strip=True) or ""


def _entry_time(node) -> datetime | None:
    for name in ("pubDate", "published", "updated", "dc:date"):
        raw = _tag_text(node, name)
        if not raw:
            continue
        for parse in (_rfc822, _iso8601):
            dt = parse(raw)
            if dt:
                return dt
    return None


def _rfc822(raw: str) -> datetime | None:
    try:
        dt = parsedate_to_datetime(raw)
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _iso8601(raw: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


# -------------------------- sources -------------------------------------
class CoindeskRSS(_RSS):
    name = "rss_coindesk"
    url = "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml"


class CointelegraphRSS(_RSS):
    name = "rss_cointelegraph"
    url = "https://cointelegraph.com/rss"


class BitcoinMagazineRSS(_RSS):
    name = "rss_bitcoin_magazine"
    url = "https://bitcoinmagazine.com/feed"


class DecryptRSS(_RSS):
    name = "rss_decrypt"
    url = "https://decrypt.co/feed"


class TheBlockRSS(_RSS):
    name = "rss_theblock"
    url = "https://www.theblock.co/rss.xml"


class GoogleNewsCrypto(_RSS):
    """Google News crypto query — nearly always returns fresh items."""
    name = "rss_google_news"
    url = (
        "https://news.google.com/rss/search"
        "?q=bitcoin+OR+ethereum+OR+crypto+when:6h"
        "&hl=en-US&gl=US&ceid=US:en"
    )


class RedditCryptoRSS(_RSS):
    name = "rss_reddit_crypto"
    url = "https://www.reddit.com/r/CryptoCurrency/hot/.rss?limit=50"
