"""Rough US market-hours check (does not account for market holidays)."""

from datetime import datetime, time
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)


def is_market_hours(now: datetime | None = None) -> bool:
    """True on weekdays between 9:30 and 16:00 America/New_York.

    Note: this does NOT check US market holidays (e.g. Thanksgiving, July 4th).
    On holidays the workflow will still run and simply find no meaningful
    price moves or news, so it's low-risk to leave as-is, but you can extend
    this with a holiday calendar if you want to skip those runs entirely.
    """
    now = now.astimezone(EASTERN) if now else datetime.now(EASTERN)
    if now.weekday() >= 5:  # Sat/Sun
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE
