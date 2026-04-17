"""Shared news types + fetcher protocol."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Literal, Protocol

NewsKind = Literal["headline", "econ"]
Impact = Literal["low", "medium", "high"]


@dataclass
class NewsItem:
    source: str
    kind: NewsKind
    title: str
    url: str
    published_at: datetime          # always UTC
    tickers: List[str] = field(default_factory=list)
    impact: Impact = "low"
    raw: dict = field(default_factory=dict)

    def short(self) -> str:
        ts = self.published_at.strftime("%m-%d %H:%MZ")
        tick = f" [{','.join(self.tickers)}]" if self.tickers else ""
        imp = f" ({self.impact})" if self.kind == "econ" else ""
        return f"{ts}{imp}{tick} {self.title}"


class Fetcher(Protocol):
    name: str
    async def fetch(self, lookback_min: int) -> List[NewsItem]: ...
