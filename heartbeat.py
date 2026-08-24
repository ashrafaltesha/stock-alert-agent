"""Tells an outside service that this bot is still alive.

Why
---
On 2026-08-24 four separate things were broken -- the arm job cancelled
itself, SEC fetching had never worked, the Telegram listener was absent for
half the day, and trades were being discarded after being acknowledged --
and *every Actions run was green*. Each one was found because a human was
expecting a specific message that did not arrive.

That is the only failure detector this system has had, and it does not
scale: it catches the alerts you are waiting for, and nothing else. The
alert you are not waiting for is exactly the one worth having.

A dead-man's switch inverts the question. Instead of watching for failure,
something outside GitHub expects a regular signal of success and complains
when it stops. It cannot be fooled by a run that reports success while doing
nothing, because a cancelled run sends no ping at all.

How
---
healthchecks.io, free for 20 checks. One secret, HEALTHCHECK_PING_KEY, and
each caller picks its own slug; the check is created on first ping via
`?create=1`, so there is nothing to configure by hand.

Deliberately fire-and-forget: a short timeout, every exception swallowed, no
retry. A monitoring call that can break the thing it monitors is worse than
no monitoring at all.

Standard library only, so the earnings watcher can use it without a pip
install.
"""

import os
import urllib.error
import urllib.request

BASE = "https://hc-ping.com"
TIMEOUT = 5

# Slugs are stable names, not run ids: healthchecks tracks the GAP between
# pings, so the same slug must be used by every run of a given workflow.
MONITOR = "stock-monitor"
LISTENER = "stock-telegram-listener"
EARNINGS_ARM = "stock-earnings-arm"


def _key():
    return os.environ.get("HEALTHCHECK_PING_KEY", "").strip()


def ping(slug: str, fail: bool = False) -> bool:
    """Signal liveness for `slug`. Returns True if the ping was accepted.

    `fail=True` reports a failure explicitly, which starts the alert
    immediately rather than waiting for the check's grace period to run out.
    """
    key = _key()
    if not key:
        return False   # not configured; silent by design

    url = f"{BASE}/{key}/{slug}"
    if fail:
        url += "/fail"
    url += "?create=1"

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "personal-stock-alerts/1.0"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        # Never fatal, never retried. This is instrumentation.
        print(f"heartbeat {slug}: {type(e).__name__}: {e}")
        return False
