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
import shutil
import subprocess
import tempfile
import time

BOT_NAME = "stock-agent-bot"
BOT_EMAIL = "actions@github.com"

# state.json is the only file two workflows both write, so it is the only one
# needing a real merge. tickers.json and watchlist.json are written solely by
# the Telegram command path, so ours are kept wholesale.
MERGED_FILE = "state.json"

MAX_ATTEMPTS = 5


def _git(*args, check=True, capture=True, cwd=None):
    return subprocess.run(
        ["git", "-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}", *args],
        check=check, capture_output=capture, text=True, cwd=cwd)


def _has_staged_changes(cwd=None) -> bool:
    return _git("diff", "--cached", "--quiet", check=False, cwd=cwd).returncode != 0


def _read_blob(ref: str, path: str, fallback: str = "{}", cwd=None) -> str:
    result = _git("show", f"{ref}:{path}", check=False, cwd=cwd)
    return result.stdout if result.returncode == 0 else fallback


def refresh_from_origin(path: str, cwd=None) -> bool:
    """Pull the newest version of ONE file from origin/main into the tree.

    A long-running job checks out once and then holds whatever it read. The
    Telegram listener loads state.json at start and keeps it for hours while
    the monitor rewrites the same file every minute -- so the listener's copy
    is stale almost immediately.

    Merging only on push REJECTION does not save this. If the listener
    happens to be level with the tip when it pushes, git accepts the write
    with no conflict at all, and everything the monitor changed since is
    silently reverted. That is exactly what happened on 2026-08-24: the
    monitor pruned eight retired keys, logged it, and the listener put them
    straight back.

    Only touches `path`, never the working tree's code, so a running loop is
    not swapped out from underneath itself.
    """
    try:
        _git("fetch", "--quiet", "origin", "main", cwd=cwd)
        result = _git("checkout", "origin/main", "--", path, check=False, cwd=cwd)
        return result.returncode == 0
    except Exception as e:
        # Proceeding with a stale copy is worse than ideal but not fatal:
        # the merge-on-rejection path is still there as a second line.
        print(f"Could not refresh {path} from origin: {type(e).__name__}: {e}")
        return False


def commit_and_push(paths, message: str, cwd=None, merged_file=MERGED_FILE) -> bool:
    """Stage `paths`, commit, and push, merging by key on rejection.

    `cwd` selects the repository -- the public one by default, or the private
    data repo checked out at data/ for holdings.json.

    `merged_file` is the one file both sides may have touched. Merging by key
    rather than rebasing matters for holdings just as much as for state: a
    rebase conflicts on the whole file and, when it fails, silently drops the
    trade that caused it. Both files are flat JSON dictionaries, so a
    three-way key merge is exact -- our changed keys applied over theirs.

    Returns True if something was pushed, False if there was nothing to do.
    Never raises: a failed push is worth logging loudly, but it is not worth
    killing a listener that is otherwise answering messages correctly.
    """
    root = cwd or "."
    existing = [p for p in paths if os.path.exists(os.path.join(root, p))]
    if not existing:
        return False

    merger = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "merge_state.py")
    try:
        _git("add", *existing, cwd=cwd)
        if not _has_staged_changes(cwd):
            return False
        _git("commit", "-m", message, cwd=cwd)

        for attempt in range(1, MAX_ATTEMPTS + 1):
            if _git("push", check=False, cwd=cwd).returncode == 0:
                print(f"Pushed: {message}")
                return True

            print(f"Push rejected (attempt {attempt}/{MAX_ATTEMPTS}) -- "
                  f"merging {merged_file} by key before retry.")
            _git("fetch", "origin", "main", cwd=cwd)

            base = _git("merge-base", "HEAD", "origin/main", cwd=cwd).stdout.strip()
            base_json = _read_blob(base, merged_file, cwd=cwd)
            theirs_json = _read_blob("origin/main", merged_file, cwd=cwd)
            ours_json = _read_blob("HEAD", merged_file, cwd=cwd)
            # Files only this process writes survive the reset verbatim.
            ours_other = {p: _read_blob("HEAD", p, fallback="", cwd=cwd)
                          for p in existing if p != merged_file}

            _git("reset", "--hard", "origin/main", cwd=cwd)

            for path, content in ours_other.items():
                if content:
                    with open(os.path.join(root, path), "w") as fh:
                        fh.write(content)

            tmp = tempfile.mkdtemp()
            names = {}
            for label, content in (("base", base_json), ("ours", ours_json),
                                   ("theirs", theirs_json)):
                names[label] = os.path.join(tmp, f"{label}.json")
                with open(names[label], "w") as fh:
                    fh.write(content)

            subprocess.run(
                ["python3", merger,
                 "--base", names["base"], "--ours", names["ours"],
                 "--theirs", names["theirs"],
                 "--out", os.path.join(root, merged_file)],
                check=True)
            shutil.rmtree(tmp, ignore_errors=True)

            _git("add", *existing, cwd=cwd)
            if not _has_staged_changes(cwd):
                print("Nothing left to commit after merge -- "
                      "the other run covered it.")
                return False
            _git("commit", "-m", message, cwd=cwd)
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
