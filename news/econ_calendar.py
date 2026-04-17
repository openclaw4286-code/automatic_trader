"""Economic-calendar scrapers (ForexFactory + Investing.com fallback).

These feeds do not expose stable JSON APIs; we scrape the public
calendar pages and normalise into NewsItem(kind='econ'). High-impact
USD/CPI/FOMC events are the ones that matter for crypto majors.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List

import httpx
from bs4 import BeautifulSoup

from news.base import Impact, NewsItem


_UA = {"User-Agent": "Mozilla/5.0 (compatible; ict-trader/1.0)"}


def _impact_from_class(cls: str) -> Impact:
    cls = (cls or "").lower()
    if "high" in cls or "red" in cls:
        return "high"
    if "medium" in cls or "orange" in cls or "yellow" in cls:
        return "medium"
    return "low"


class ForexFactory:
    name = "forexfactory"
    URL = "https://www.forexfactory.com/calendar"

    async def fetch(self, lookback_min: int) -> List[NewsItem]:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers=_UA) as c:
            resp = await c.get(self.URL)
            resp.raise_for_status()
            html = resp.text
        return self._parse(html, lookback_min)

    def _parse(self, html: str, lookback_min: int) -> List[NewsItem]:
        soup = BeautifulSoup(html, "lxml")
        rows = soup.select("tr.calendar__row")
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(minutes=lookback_min)
        window_end = now + timedelta(hours=24)
        out: List[NewsItem] = []
        current_date: datetime | None = None

        for row in rows:
            date_cell = row.select_one(".calendar__date")
            if date_cell and date_cell.get_text(strip=True):
                try:
                    label = date_cell.get_text(" ", strip=True)
                    current_date = datetime.strptime(label + f" {now.year}", "%a%b %d %Y").replace(tzinfo=timezone.utc)
                except Exception:
                    pass

            time_cell = row.select_one(".calendar__time")
            title_cell = row.select_one(".calendar__event")
            currency_cell = row.select_one(".calendar__currency")
            impact_cell = row.select_one(".calendar__impact span")
            if not (title_cell and current_date):
                continue

            time_text = (time_cell.get_text(strip=True) if time_cell else "").lower()
            try:
                ts = current_date
                if time_text and time_text not in ("all day", "tentative", ""):
                    ts = datetime.strptime(time_text, "%I:%M%p").replace(
                        year=current_date.year,
                        month=current_date.month,
                        day=current_date.day,
                        tzinfo=timezone.utc,
                    )
            except Exception:
                ts = current_date

            if not (window_start <= ts <= window_end):
                continue

            impact = _impact_from_class(impact_cell.get("class", [""])[0] if impact_cell else "")
            title = title_cell.get_text(" ", strip=True)
            ccy = currency_cell.get_text(strip=True) if currency_cell else ""

            out.append(
                NewsItem(
                    source=self.name,
                    kind="econ",
                    title=f"{ccy} {title}" if ccy else title,
                    url=self.URL,
                    published_at=ts,
                    impact=impact,
                )
            )
        return out


class InvestingCalendar:
    name = "investing_economic"
    URL = "https://www.investing.com/economic-calendar/"

    async def fetch(self, lookback_min: int) -> List[NewsItem]:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers=_UA) as c:
            resp = await c.get(self.URL)
            resp.raise_for_status()
            html = resp.text
        return self._parse(html, lookback_min)

    def _parse(self, html: str, lookback_min: int) -> List[NewsItem]:
        soup = BeautifulSoup(html, "lxml")
        rows = soup.select("tr.js-event-item")
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(minutes=lookback_min)
        window_end = now + timedelta(hours=24)
        out: List[NewsItem] = []
        for row in rows:
            ts_attr = row.get("data-event-datetime") or ""
            try:
                ts = datetime.strptime(ts_attr, "%Y/%m/%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if not (window_start <= ts <= window_end):
                continue
            title_el = row.select_one("td.event a")
            impact_el = row.select_one("td.sentiment")
            ccy_el = row.select_one("td.flagCur")
            title = title_el.get_text(strip=True) if title_el else ""
            ccy = ccy_el.get_text(strip=True) if ccy_el else ""
            impact = "low"
            if impact_el:
                bulls = impact_el.select("i.grayFullBullishIcon")
                impact = "high" if len(bulls) >= 3 else ("medium" if len(bulls) == 2 else "low")
            out.append(
                NewsItem(
                    source=self.name,
                    kind="econ",
                    title=f"{ccy} {title}".strip(),
                    url=self.URL,
                    published_at=ts,
                    impact=impact,
                )
            )
        return out
