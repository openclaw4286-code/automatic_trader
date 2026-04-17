"""KST trading-session window checker."""
from __future__ import annotations

from datetime import datetime, time

from config import KST, SESSIONS_KST


def _parse(hhmm: str) -> time:
    h, m = hhmm.split(":")
    return time(int(h), int(m))


def in_session(now: datetime | None = None) -> bool:
    now = (now or datetime.now(KST)).astimezone(KST)
    t = now.time()
    for start_s, end_s in SESSIONS_KST:
        start, end = _parse(start_s), _parse(end_s)
        if start <= end:
            if start <= t <= end:
                return True
        else:  # wraps past midnight
            if t >= start or t <= end:
                return True
    return False


def current_window(now: datetime | None = None) -> tuple[str, str] | None:
    now = (now or datetime.now(KST)).astimezone(KST)
    t = now.time()
    for start_s, end_s in SESSIONS_KST:
        start, end = _parse(start_s), _parse(end_s)
        hit = (start <= t <= end) if start <= end else (t >= start or t <= end)
        if hit:
            return start_s, end_s
    return None
