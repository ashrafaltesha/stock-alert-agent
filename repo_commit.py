"""Commit and push repo files from inside a running script.

Why this exists
---------------
Every workflow used to do this in YAML, as a shell step that runs after the
Python process exits. That is fine for a one-shot job, but it makes two
things impossible:

1. A LONG-RUNNING job cannot persist anything until it finishes. The Telegram
   listener loops for an hour; a watchlist edit made in the first minute would
   sit uncommitted for 59 more, and a crash would lose it.

2. Nothing can happen AFTER the push. That ordering matters: arming an
   earnings watch dispatches the watcher workflow, and the watcher reads
   state.json from a fresh checkout. Dispatching before the push means the
   watcher can check out a state.json with no armed watch, find nothing to do,
   and exit -- leaving the report uncovered until the next hourly run.

Both problems disappear if the process can commit at a point of its choosing.

The merge, not a rebase
-----------------------
Same reasoning as the YAML version this replaces. monitor.yml rewrites the
whole of state.json every few minutes, so a rebase conflicts on every line,
every time. Instead: take origin/main, then re-apply only what this process
changed relative to the commit it started from -- including DELETIONS, so a
resolved ew_watch:: entry stays deleted rather than reviving into a repeat
earnings alert.
"""

import os
import random
import subprocess
import time

BOT_NAME = "stock-agent-bot"
BOT_EMAIL = "actions@github.com"

# state.json is the only file two workflows both write, so it is the only one
# needing a real merge. tickers.json and watchlist.json are written solely by
# the Telegram command path, so ours are kept wholesale.
MERGED_FILE = "state.json"

MAX_ATTEMPTS = 5


def _git(*args, check=True, capture=True):
    return subprocess.run(
        ["git", "-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}", *args],
        check=check, capture_output=capture, text=True)


def _has_staged_changes() -> bool:
    return _git("diff", "--cached", "--quiet", check=False).returncode != 0


def _read_blob(ref: str, path: str, fallback: str = "{}") -> str:
    result = _git("show", f"{ref}:{path}", check=False)
    return result.stdout if result.returncode == 0 else fallback


def commit_and_push(paths, message: str) -> bool:
    """Stage `paths`, commit, and push, merging state.json on rejection.

    Returns True if something was pushed, False if there was nothing to do.
    Never raises: a failed push is worth logging loudly, but it is not worth
    killing a listener that is otherwise answering messages correctly.
    """
    existing = [p for p in paths if os.path.exists(p)]
    if not existing:
        return False

    try:
        _git("add", *existing)
        if not _has_staged_changes():
            return False
        _git("commit", "-m", message)

        for attempt in range(1, MAX_ATTEMPTS + 1):
            if _git("push", check=False).returncode == 0:
                print(f"Pushed: {message}")
                return True

            print(f"Push rejected (attempt {attempt}/{MAX_ATTEMPTS}) -- "
                  f"merging {MERGED_FILE} by key before retry.")
            _git("fetch", "origin", "main")

            base = _git("merge-base", "HEAD", "origin/main").stdout.strip()
            base_json = _read_blob(base, MERGED_FILE)
            theirs_json = _read_blob("origin/main", MERGED_FILE)
            ours_json = _read_blob("HEAD", MERGED_FILE)
            # Files only this process writes survive the reset verbatim.
            ours_other = {p: _read_blob("HEAD", p, fallback="")
                          for p in existing if p != MERGED_FILE}

            _git("reset", "--hard", "origin/main")

            for path, content in ours_other.items():
                if content:
                    with open(path, "w") as fh:
                        fh.write(content)

            for name, content in (("/tmp/base.json", base_json),
                                  ("/tmp/ours.json", ours_json),
                                  ("/tmp/theirs.json", theirs_json)):
                with open(name, "w") as fh:
                    fh.write(content)

            subprocess.run(
                ["python3", "merge_state.py",
                 "--base", "/tmp/base.json", "--ours", "/tmp/ours.json",
                 "--theirs", "/tmp/theirs.json", "--out", MERGED_FILE],
                check=True)

            _git("add", *existing)
            if not _has_staged_changes():
                print("Nothing left to commit after merge -- "
                      "the other run covered it.")
                return False
            _git("commit", "-m", message)
            # Jittered, so two workflows colliding do not retry in lockstep.
            time.sleep(random.uniform(1, 5))

        print(f"Push FAILED after {MAX_ATTEMPTS} attempts: {message}")
        return False

    except subprocess.CalledProcessError as e:
        print(f"git failed: {e.cmd}\n{(e.stderr or '').strip()[:400]}")
        return False
    except Exception as e:
        print(f"commit_and_push: {type(e).__name__}: {e}")
        return False
