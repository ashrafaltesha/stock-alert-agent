"""Records when each part of the system last did its job, and reports it.

Why
---
On 2026-08-24 four things were broken at once and every Actions run was
green. Answering "is it working?" meant opening the Actions tab, finding the
right workflow, expanding a step, and reading a log -- repeatedly, for hours.
Meanwhile the one question that mattered had a short answer: which components
have done something recently, and which have gone quiet.

This writes that down as it happens, so the answer can be a Telegram message
instead of an investigation.

It is NOT a dead-man's switch. Nothing here notices a failure on its own --
you have to ask. It closes the "I had to go read logs" gap, not the "nobody
told me it stopped" gap.

Design
------
Timestamps live in state.json under `hb::` and `alert::` keys, so there is no
new store and no new service. They are written at the point the work actually
completes, never at the point it starts: a component that logs "beginning" and
then dies would otherwise look healthy.
"""

from datetime import datetime

from timeutil import EASTERN, now_et

HB_PREFIX = "hb::"
ALERT_PREFIX = "alert::"

# What "quiet" means for each component, in minutes. Past this it is reported
# as stale rather than healthy.
EXPECTED_INTERVAL = {
    "monitor": 20,        # external cron every ~5 min, generous margin
    "listener": 25,       # pings itself roughly every 10 min while idle
    "earnings_arm": 60 * 26,   # twice daily; a full day plus slack
    "earnings_watch": 90,      # only while something is armed
}


def record(state: dict, component: str) -> None:
    """Mark `component` as having just completed a unit of work."""
    state[f"{HB_PREFIX}{component}"] = now_et().isoformat()


def record_alert(state: dict, kind: str) -> None:
    """Mark that an alert of `kind` (news/price/earnings) was actually sent."""
    state[f"{ALERT_PREFIX}{kind}"] = now_et().isoformat()


def _age_minutes(stamp: str):
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=EASTERN)
    return (now_et() - when).total_seconds() / 60


def _human(minutes) -> str:
    if minutes is None:
        return "never"
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{int(minutes)}m ago"
    if minutes < 60 * 24:
        return f"{minutes / 60:.1f}h ago"
    return f"{minutes / 1440:.1f}d ago"


def component_lines(state: dict):
    """[(name, human_age, ok)] for each component we track."""
    out = []
    for component, limit in EXPECTED_INTERVAL.items():
        age = _age_minutes(state.get(f"{HB_PREFIX}{component}"))
        # A watcher that has never run is not a fault: it exits when nothing
        # is armed, which is most days.
        ok = age is not None and age <= limit
        if component == "earnings_watch" and age is None:
            ok = True
        out.append((component, _human(age), ok))
    return out


def alert_lines(state: dict):
    return [(kind, _human(_age_minutes(state.get(f"{ALERT_PREFIX}{kind}"))))
            for kind in ("earnings", "news", "price")]
