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
import urllib.error
import urllib.request

WORKFLOW = "earnings_watch.yml"


def start_earnings_watcher() -> bool:
    """Dispatch the watcher workflow. Returns True if accepted."""
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        # Running locally, or the workflow didn't grant actions: write.
        print("No GITHUB_TOKEN/GITHUB_REPOSITORY; skipping watcher dispatch.")
        return False

    url = (f"https://api.github.com/repos/{repo}/actions/workflows/"
           f"{WORKFLOW}/dispatches")
    body = json.dumps({"ref": os.environ.get("GITHUB_REF_NAME", "main")}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"Earnings watcher dispatched (HTTP {resp.status}).")
            return True
    except urllib.error.HTTPError as e:
        print(f"Watcher dispatch failed: HTTP {e.code} {e.read()[:200]!r}")
    except Exception as e:
        print(f"Watcher dispatch failed: {type(e).__name__}: {e}")
    return False
