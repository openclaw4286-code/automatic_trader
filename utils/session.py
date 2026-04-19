"""KST trading-session window checker + weekend boundary helpers.

A tick is 'in session' only when both (a) it falls inside one of the
configured time windows and (b) the KST weekday is in TRADE_WEEKDAYS_KST.
For windows that wrap past midnight we evaluate each side against its
own calendar date, so a Friday 22:30-01:00 window still trades into
early Saturday morning while a Saturday evening window is blocked.

The weekend helpers compute the exact clock moment when the current
trading week's final session ends (= weekend starts) and whether we
are inside the pre-weekend freeze lead.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Optional

from config import KST, SESSIONS_KST, TRADE_WEEKDAYS_KST, WEEKEND_FREEZE_LEAD_MIN


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


# ---------------------------------------------------------------- weekend
def _last_trade_weekday() -> int:
    """Highest weekday id that trades (e.g. 4 = Friday for Mon-Fri)."""
    return max(TRADE_WEEKDAYS_KST) if TRADE_WEEKDAYS_KST else 4


def weekend_start_at(now: datetime | None = None) -> Optional[datetime]:
    """Return the datetime of the NEXT weekend-freeze boundary.

    That is: the latest session end on the current trading week's last
    trade day (Friday by default), rolling forward by 7 days if that
    moment has already fully passed. This way
    `weekend_start_at(prev_tick) < weekend_start_at(prev_tick) <= now`
    remains monotone across the crossing tick, which is what
    `crossed_weekend_boundary` relies on.
    """
    if not TRADE_WEEKDAYS_KST:
        return None
    now = (now or datetime.now(KST)).astimezone(KST)
    last_day = _last_trade_weekday()

    # days_to_last can be negative (past Friday this week) — that's OK,
    # we still compute THIS week's boundary, then bump forward by 7 days
    # if we are already past it.
    days_to_last = last_day - now.weekday()
    target_date = (now + timedelta(days=days_to_last)).date()

    def _latest_end_on(date):
        latest: Optional[datetime] = None
        for start_s, end_s in SESSIONS_KST:
            start, end = _parse(start_s), _parse(end_s)
            if start <= end:
                end_dt = datetime.combine(date, end, tzinfo=KST)
            else:
                # wraps into date + 1
                end_dt = datetime.combine(date + timedelta(days=1), end, tzinfo=KST)
            if latest is None or end_dt > latest:
                latest = end_dt
        return latest

    end = _latest_end_on(target_date)
    if end is not None and end < now:
        end = _latest_end_on(target_date + timedelta(days=7))
    return end


def minutes_until_weekend(now: datetime | None = None) -> float:
    """Minutes until weekend_start (can be negative if already past)."""
    ws = weekend_start_at(now)
    if ws is None:
        return float("inf")
    now = (now or datetime.now(KST)).astimezone(KST)
    return (ws - now).total_seconds() / 60.0


def in_pre_weekend_freeze(now: datetime | None = None) -> bool:
    """True iff we are within the WEEKEND_FREEZE_LEAD_MIN minutes before
    weekend_start. No new entries during this window."""
    m = minutes_until_weekend(now)
    return 0 <= m <= WEEKEND_FREEZE_LEAD_MIN


def crossed_weekend_boundary(prev_now: datetime, now: datetime) -> bool:
    """True iff weekend_start falls in the half-open interval (prev_now, now].
    Used to trigger the one-shot flatten on the monitor tick that first
    observes the transition."""
    ws = weekend_start_at(prev_now)
    if ws is None:
        return False
    return prev_now < ws <= now
