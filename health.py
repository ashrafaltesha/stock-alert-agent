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
    # See EXIT_PERSISTED. 330-minute loop plus a margin for the runner and
    # the commit, used only when the platform cannot be asked.
    "earnings_watch": 350,
}

# Components whose heartbeat reaches state.json only when their run ENDS.
#
# The watcher records a heartbeat every 15-second cycle, but `save_state`
# writes a local file and earnings_watch.yml commits it in a single step
# after the loop -- deliberately, because a git push inside a 15-second poll
# would cost more than the poll interval. So the recorded timestamp measures
# time since the last run EXITED, not time since the last poll, and a
# perfectly healthy watcher looks 330 minutes stale for most of its life.
#
# The old limit of 90 minutes therefore reported a working watcher as broken
# for four hours out of every five and a half. I used that same number to
# judge liveness during today's incident and nearly called a healthy run
# dead.
#
# The rule, which is not specific to this component: a value that is only
# persisted at exit cannot measure liveness. Ask the platform that owns the
# process instead, and keep the timestamp as a fallback for when it cannot
# be reached.
EXIT_PERSISTED = {"earnings_watch": "earnings_watch.yml"}


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


def _duration(minutes) -> str:
    """An elapsed span, as opposed to _human's "how long ago"."""
    if minutes is None:
        return "an unknown time"
    if minutes < 1:
        return "under a minute"
    if minutes < 60:
        return f"{int(minutes)}m"
    return f"{minutes / 60:.1f}h"


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


def live_status(component: str, state: dict, latest_run):
    """(text, ok) for an EXIT_PERSISTED component, or None to fall through.

    `latest_run` is a callable taking a workflow filename and returning
    (status, age_minutes), or None when the platform cannot be asked --
    workflow_trigger._latest_run has exactly that shape. It is injected
    rather than imported so this module stays free of the dispatch code and
    so tests can drive every branch without a network.

    What matters is not whether the watcher is running, but whether it is
    running WHEN SOMETHING IS ARMED. Idle is the correct state on the ~215
    trading days a year with nothing to watch; the fault is a watch armed
    with nothing polling, which is what cost the NVDA report.
    """
    workflow = EXIT_PERSISTED.get(component)
    if workflow is None or latest_run is None:
        return None
    try:
        latest = latest_run(workflow)
    except Exception:
        return None
    if latest is None:
        return None                       # cannot tell; use the heartbeat

    status, age = latest
    armed = sum(1 for k in state if k.startswith("ew_watch::"))

    if status in ("in_progress", "queued", "waiting", "requested", "pending"):
        return f"polling, up {_duration(age)}", True
    if armed:
        plural = "es" if armed != 1 else ""
        return f"NOT RUNNING, {armed} watch{plural} armed", False
    return "idle (nothing armed)", True


def component_lines(state: dict, latest_run=None):
    """[(name, human_age, ok)] for each component we track.

    `latest_run` is optional: callers that can reach the Actions API pass
    workflow_trigger._latest_run so EXIT_PERSISTED components are judged on
    what the platform reports rather than on a timestamp that only moves
    when the run ends.
    """
    out = []
    for component, limit in EXPECTED_INTERVAL.items():
        live = live_status(component, state, latest_run)
        if live is not None:
            detail, ok = live
            out.append((component, detail, ok))
            continue

        age = _age_minutes(state.get(f"{HB_PREFIX}{component}"))
        # A watcher that has never run is not a fault: it exits when nothing
        # is armed, which is most days.
        ok = age is not None and age <= limit
        if component in EXIT_PERSISTED and age is None:
            ok = True
        out.append((component, _human(age), ok))
    return out


def alert_lines(state: dict):
    return [(kind, _human(_age_minutes(state.get(f"{ALERT_PREFIX}{kind}"))))
            for kind in ("earnings", "news", "price")]
