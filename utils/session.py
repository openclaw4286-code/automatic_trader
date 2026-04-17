"""KST trading-session window checker.

A tick is 'in session' only when both (a) it falls inside one of the
configured time windows and (b) the KST weekday is in TRADE_WEEKDAYS_KST.
For windows that wrap past midnight we evaluate each side against its
own calendar date, so a Friday 22:30-01:00 window still trades into
early Saturday morning while a Saturday evening window is blocked.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

from config import KST, SESSIONS_KST, TRADE_WEEKDAYS_KST


def _parse(hhmm: str) -> time:
    h, m = hhmm.split(":")
    return time(int(h), int(m))


def in_session(now: datetime | None = None) -> bool:
    now = (now or datetime.now(KST)).astimezone(KST)
    t = now.time()
    today_ok = now.weekday() in TRADE_WEEKDAYS_KST
    prev_ok = (now - timedelta(days=1)).weekday() in TRADE_WEEKDAYS_KST

    for start_s, end_s in SESSIONS_KST:
        start, end = _parse(start_s), _parse(end_s)
        if start <= end:
            if start <= t <= end and today_ok:
                return True
        else:  # wraps past midnight
            if t >= start and today_ok:
                return True
            if t <= end and prev_ok:
                return True
    return False


def current_window(now: datetime | None = None) -> tuple[str, str] | None:
    now = (now or datetime.now(KST)).astimezone(KST)
    if not in_session(now):
        return None
    t = now.time()
    for start_s, end_s in SESSIONS_KST:
        start, end = _parse(start_s), _parse(end_s)
        hit = (start <= t <= end) if start <= end else (t >= start or t <= end)
        if hit:
            return start_s, end_s
    return None
