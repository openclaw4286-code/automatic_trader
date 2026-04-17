"""CryptoPanic REST fetcher (requires CRYPTOPANIC_TOKEN)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List

import httpx

from config import CRYPTOPANIC_TOKEN
from news.base import NewsItem


class CryptoPanic:
    name = "cryptopanic"
    URL = "https://cryptopanic.com/api/v1/posts/"

    async def fetch(self, lookback_min: int) -> List[NewsItem]:
        if not CRYPTOPANIC_TOKEN:
            raise RuntimeError("CRYPTOPANIC_TOKEN missing")

        params = {
            "auth_token": CRYPTOPANIC_TOKEN,
            "kind": "news",
            "public": "true",
            "filter": "hot",
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(self.URL, params=params)
            resp.raise_for_status()
            data = resp.json()

        return self._parse(data, lookback_min)

    def _parse(self, data: dict, lookback_min: int) -> List[NewsItem]:
        out: List[NewsItem] = []
        cutoff = datetime.now(timezone.utc).timestamp() - lookback_min * 60
        for row in data.get("results", []):
            pub = row.get("published_at") or row.get("created_at")
            if not pub:
                continue
            try:
                ts = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            except ValueError:
                continue
            if ts.timestamp() < cutoff:
                continue
            tickers = [c.get("code", "") for c in row.get("currencies") or []]
            out.append(
                NewsItem(
                    source=self.name,
                    kind="headline",
                    title=row.get("title", ""),
                    url=row.get("url") or row.get("source", {}).get("url", ""),
                    published_at=ts.astimezone(timezone.utc),
                    tickers=[t for t in tickers if t],
                    raw=row,
                )
            )
        return out
