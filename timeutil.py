"""Eastern-time helpers and the watch record. STANDARD LIBRARY ONLY.

Why this module exists
----------------------
The earnings watcher's whole latency advantage comes from running with no
`pip install`: the workflow does a checkout and then `python3
earnings_watch.py poll`, which saves 25-55 seconds of startup on the one path
where latency is the product.

That only works if NOTHING the watcher imports reaches a third-party package.
It did. `earnings_watch` imported `earnings_utils` for four small helpers --
EASTERN, now_et, date_str_et, arm_earnings_watch, none of which need anything
outside the standard library -- and `earnings_utils` imports `requests` and
`yfinance` at module level for the calendar functions the watcher never
calls. One import line dragged the entire dependency tree into a job that
installs none of it.

So the stdlib-safe pieces live here, and `earnings_utils` re-exports them so
its other callers are unaffected. The calendar functions, which genuinely
need requests, stay there and are imported lazily by `arm()` -- which runs in
the job that does have them.

The rule this encodes: a module the watcher imports may not import anything
outside the standard library, transitively. tests/test_core_logic.py enforces
it by importing earnings_watch with third-party modules blocked.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")


def now_et() -> datetime:
    return datetime.now(EASTERN)


def date_str_et(offset_days: int = 0) -> str:
    """YYYY-MM-DD for today (offset=0) or another day, in America/New_York."""
    return (now_et() + timedelta(days=offset_days)).strftime("%Y-%m-%d")


def arm_earnings_watch(state: dict, ticker: str, hours: int) -> bool:
    """Create the watch record the poll loop acts on.

    The single point where a watch is armed, whether it came from you texting
    "earnings for X" or from a holding turning up on a calendar. Detection
    then runs through one code path rather than the holdings path quietly
    using a weaker one.

    Returns True only if a NEW watch was created. An existing watch is left
    alone rather than refreshed, because its record carries what has already
    been sent -- resetting it would re-send everything.
    """
    key = f"ew_watch::{ticker.upper()}"
    if key in state:
        return False
    now = now_et()
    state[key] = {
        "armed": now.isoformat(),
        "expires": (now + timedelta(hours=hours)).isoformat(),
    }
    return True
