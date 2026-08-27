"""Starts the earnings watcher on demand.

The watcher exits when nothing is armed rather than idling -- on most days
there is nothing to watch, and holding a runner for eleven hours to do
nothing is both wasteful and hard to justify under GitHub's terms.

That is only safe if arming can START a watcher. Without this, texting
"earnings for TPR" at 09:30 would wait until the next hourly run at 10:05
before anything polled.

Uses the GITHUB_TOKEN that Actions injects, so there is no new secret to
manage. Failure is deliberately non-fatal: the worst case is the delay this
exists to avoid, which is a nuisance rather than a lost alert.
"""

import json
import os
from datetime import datetime, timezone
import urllib.error
import urllib.request

WORKFLOW = "earnings_watch.yml"
LISTENER_WORKFLOW = "telegram_commands.yml"


def _credentials():
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        # Running locally, or the workflow didn't grant actions: write.
        return None, None
    return token, repo


def _api(url, method="GET", body=None):
    token, _ = _credentials()
    req = urllib.request.Request(
        url, data=body, method=method, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        })
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = resp.read()
        return resp.status, (json.loads(payload) if payload else {})


def _dispatch(workflow: str, label: str) -> bool:
    token, repo = _credentials()
    if not token:
        print(f"No GITHUB_TOKEN/GITHUB_REPOSITORY; skipping {label} dispatch.")
        return False
    url = (f"https://api.github.com/repos/{repo}/actions/workflows/"
           f"{workflow}/dispatches")
    body = json.dumps({"ref": os.environ.get("GITHUB_REF_NAME", "main")}).encode()
    try:
        status, _ = _api(url, method="POST", body=body)
        print(f"{label} dispatched (HTTP {status}).")
        return True
    except urllib.error.HTTPError as e:
        print(f"{label} dispatch failed: HTTP {e.code} {e.read()[:200]!r}")
    except Exception as e:
        print(f"{label} dispatch failed: {type(e).__name__}: {e}")
    return False


def start_earnings_watcher() -> bool:
    """Dispatch the watcher workflow. Returns True if accepted."""
    return _dispatch(WORKFLOW, "Earnings watcher")


def ensure_watcher_running(state: dict) -> bool:
    """Start the earnings watcher if something is armed and nothing is polling.

    This gap cost a real report. On 2026-08-26 NVDA was armed correctly, its
    baseline was set correctly, and both of its 8-Ks were newer than that
    baseline -- fully detectable. But the last watcher run started at 10:29
    and its 330-minute loop ended at 15:59, NVDA filed at 16:21, and no
    scheduled run followed. Nothing was polling. Missed by 22 minutes.

    The listener already had this protection; the watcher did not. The same
    unreliable scheduler drives both, and raising LOOP_MINUTES to 330 made
    each run cover a fixed block -- so a missed cron stops coverage outright
    rather than merely delaying it.

    Gated on `ew_watch::` because the watcher exits when nothing is armed:
    dispatching on an empty state would start a runner to do nothing, every
    minute, forever.
    """
    armed = [k for k in state if k.startswith("ew_watch::")]
    if not armed:
        return False

    if not _should_dispatch(WORKFLOW):
        return False

    tickers = ", ".join(sorted(k.split("::", 1)[1] for k in armed))
    print(f"Watches armed ({tickers}) but no watcher running -- starting one.")
    return _dispatch(WORKFLOW, "Earnings watcher (watchdog)")


def restart_listener() -> bool:
    """Start a fresh listener, for when the running one is on stale code.

    Dispatched as the current listener exits, so there is no window where
    nothing is listening. Safe against the watchdog: that workflow cancels in
    progress, so at worst one of the two replaces the other.
    """
    return _dispatch(LISTENER_WORKFLOW, "Telegram listener (code updated)")


# A dispatch CANCELS the run it is meant to protect, because both watch and
# listener workflows use cancel-in-progress. So a watchdog that dispatches too
# eagerly does not heal the system, it holds it down: every minute a fresh run
# starts and kills the one before, and nothing ever polls for longer than the
# gap between checks.
#
# Observed live on 2026-08-27: watcher runs at 14:16, 14:17, 14:18, 14:19,
# 14:20 and 14:20:50, two of them "running" at once. The status check was
# returning "nothing running" while a run was in fact starting.
#
# Two API statuses are not enough to know. A run waiting on a concurrency
# lock reports neither in_progress nor queued, and the API is eventually
# consistent, so a run created seconds ago can still read as absent.
#
# The cooldown is what makes this safe rather than clever: whatever the API
# says, a workflow that was STARTED very recently is left alone. Being slow
# to restart costs one interval; restarting too often costs everything.
DISPATCH_COOLDOWN_MINUTES = 8

ACTIVE_STATUSES = {"in_progress", "queued", "waiting", "requested", "pending"}


def _latest_run(workflow: str):
    """(status, age_minutes) of the most recent run, or None if unknown.

    One request, no status filter -- so a run in a state the filters do not
    name still counts. Unknown is not the same as absent: the caller must not
    dispatch when it cannot tell.
    """
    token, repo = _credentials()
    if not token:
        return None
    url = (f"https://api.github.com/repos/{repo}/actions/workflows/"
           f"{workflow}/runs?per_page=1")
    try:
        _, data = _api(url)
        runs = data.get("workflow_runs") or []
        if not runs:
            return ("none", None)
        run = runs[0]
        created = run.get("created_at") or ""
        age = None
        if created:
            try:
                started = datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - started).total_seconds() / 60
            except ValueError:
                age = None
        return (str(run.get("status") or ""), age)
    except Exception as e:
        print(f"Could not check {workflow} runs: {type(e).__name__}: {e}")
        return None


def _should_dispatch(workflow: str) -> bool:
    """Only when we are confident nothing is running AND nothing just started."""
    latest = _latest_run(workflow)
    if latest is None:
        return False                      # cannot tell -- never guess
    status, age = latest
    if status in ACTIVE_STATUSES:
        return False
    if age is not None and age < DISPATCH_COOLDOWN_MINUTES:
        print(f"{workflow}: last run started {age:.1f} min ago; "
              f"inside the {DISPATCH_COOLDOWN_MINUTES} min cooldown.")
        return False
    return True


def ensure_listener_running() -> bool:
    """Restart the Telegram listener if nothing is currently listening.

    GitHub's scheduler is best-effort and, in practice, badly so: on
    2026-08-24 the hourly cron for the listener actually fired at 19:56,
    21:03, 23:33, 01:58, 04:29, 06:58 and 10:26 -- gaps of up to 89 minutes
    between runs that were supposed to be an hour apart. A 62-minute loop
    cannot cover a 150-minute interval, and the bot simply stops answering.

    This runs from the monitor workflow, which is driven by an external cron
    every five minutes and is therefore punctual. It only dispatches when
    nothing is running, so it cannot interrupt a healthy listener.
    """
    if not _should_dispatch(LISTENER_WORKFLOW):
        return False
    print("No Telegram listener running -- starting one.")
    return _dispatch(LISTENER_WORKFLOW, "Telegram listener")
