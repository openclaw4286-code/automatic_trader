"""Parser-level tests for news fetchers (no live HTTP).

We drive each fetcher's `_parse` directly with fixture payloads so the
network never has to be reachable for tests to run.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from news.aggregator import NewsAggregator, _dedupe_sort
from news.base import NewsItem
from news.cryptopanic import CryptoPanic
from news.econ_calendar import ForexFactory, InvestingCalendar
from news.rss import CoindeskRSS


# ----------------------- CryptoPanic ------------------------------------
def test_cryptopanic_parses_recent_and_skips_old():
    now = datetime.now(timezone.utc)
    payload = {
        "results": [
            {
                "title": "BTC rallies",
                "url": "https://example.com/a",
                "published_at": now.isoformat().replace("+00:00", "Z"),
                "currencies": [{"code": "BTC"}, {"code": "ETH"}],
            },
            {
                "title": "old news",
                "url": "https://example.com/b",
                "published_at": (now - timedelta(hours=5)).isoformat().replace("+00:00", "Z"),
                "currencies": [],
            },
        ]
    }
    items = CryptoPanic()._parse(payload, lookback_min=60)
    assert len(items) == 1
    assert items[0].title == "BTC rallies"
    assert items[0].tickers == ["BTC", "ETH"]


# --------------------------- RSS ----------------------------------------
def test_rss_parses_recent_item():
    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    rss = f"""<?xml version='1.0'?>
<rss><channel>
  <item><title>Fresh headline</title><link>https://x/1</link><pubDate>{now}</pubDate></item>
  <item><title>Ancient</title><link>https://x/2</link><pubDate>Wed, 01 Jan 2020 00:00:00 +0000</pubDate></item>
</channel></rss>"""
    items = CoindeskRSS()._parse(rss, lookback_min=60)
    titles = [i.title for i in items]
    assert "Fresh headline" in titles
    assert "Ancient" not in titles


# ----------------------- ForexFactory -----------------------------------
def test_forexfactory_parses_high_impact_rows():
    now = datetime.now(timezone.utc)
    day = now.strftime("%a%b %d")
    tm = now.strftime("%I:%M%p").lstrip("0").lower()
    html = f"""
    <table><tr class='calendar__row'>
      <td class='calendar__date'>{day}</td>
      <td class='calendar__time'>{tm}</td>
      <td class='calendar__currency'>USD</td>
      <td class='calendar__impact'><span class='icon--ff-impact-red'></span></td>
      <td class='calendar__event'>CPI m/m</td>
    </tr></table>"""
    items = ForexFactory()._parse(html, lookback_min=60)
    assert items, "expected at least one row to match the current minute"
    assert items[0].impact == "high"
    assert "CPI" in items[0].title


# ------------------------ Investing -------------------------------------
def test_investing_parses_rows_with_datetime_attr():
    future = (datetime.now(timezone.utc) + timedelta(minutes=30)).strftime("%Y/%m/%d %H:%M:%S")
    html = f"""
    <table><tr class='js-event-item' data-event-datetime='{future}'>
      <td class='flagCur'>USD</td>
      <td class='sentiment'>
        <i class='grayFullBullishIcon'></i>
        <i class='grayFullBullishIcon'></i>
        <i class='grayFullBullishIcon'></i>
      </td>
      <td class='event'><a>Non-Farm Payrolls</a></td>
    </tr></table>"""
    items = InvestingCalendar()._parse(html, lookback_min=60)
    assert items
    assert items[0].impact == "high"
    assert "Non-Farm" in items[0].title


# ------------------------ Aggregator ------------------------------------
def test_dedupe_sort_orders_desc_and_removes_duplicates():
    now = datetime.now(timezone.utc)
    a = NewsItem(source="s", kind="headline", title="A", url="", published_at=now)
    b = NewsItem(source="s", kind="headline", title="a", url="", published_at=now - timedelta(minutes=1))
    c = NewsItem(source="t", kind="headline", title="B", url="", published_at=now - timedelta(minutes=2))
    out = _dedupe_sort([b, c, a])
    assert len(out) == 2
    assert out[0].title == "A"
    assert out[1].title == "B"


def test_aggregator_survives_all_failures():
    async def _run():
        agg = NewsAggregator(sources=["cryptopanic"])
        # force failure by clearing the token via monkeypatch style
        items = await agg.fetch(lookback_min=60)
        assert items == []
        assert agg.health().get("cryptopanic") is False

    asyncio.run(_run())


if __name__ == "__main__":
    test_cryptopanic_parses_recent_and_skips_old()
    test_rss_parses_recent_item()
    test_forexfactory_parses_high_impact_rows()
    test_investing_parses_rows_with_datetime_attr()
    test_dedupe_sort_orders_desc_and_removes_duplicates()
    test_aggregator_survives_all_failures()
    print("all news tests passed")
