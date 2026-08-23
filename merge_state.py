"""Three-way merge for state.json, used when a bot push loses a race.

Why this exists
---------------
monitor.py and telegram_commands.py both read and rewrite the whole of
state.json, and they used to be forced to take turns via a shared GitHub
Actions concurrency group. That worked, but the group became saturated --
about two runs a minute arriving into a lane that serves one at a time --
and GitHub started cancelling runs to clear the queue. 163 of them.

A cancelled Telegram run had usually already sent its reply but not yet
recorded the update as handled, so the next run answered the same message
again. That is where the duplicate replies came from.

The fix is to give each workflow its own concurrency group so they stop
cancelling each other. But then they can push at the same time, and the
loser's push is rejected. Rebasing is not an option: both sides rewrote
every line of the same JSON file, so a rebase conflicts every time.

So instead of replaying commits, merge the data. Git's own three-way merge
model applies cleanly at the level of JSON keys:

    base    state.json at the commit this run checked out
    ours    what this run produced
    theirs  what is on origin/main now

Take `theirs`, then apply exactly what this run changed relative to `base`.
Nothing the other workflow wrote is lost, and nothing this run did is lost.

Deletions are handled explicitly and that matters. earnings_watch.py
deletes an `ew_watch::TICKER` key once the watch expires or is satisfied.
Merging by overlay alone would let `theirs` reinstate that key, the watch
would come back to life, and you would get the same earnings alert again --
the exact bug class this whole change is meant to end.

Conflicts (both sides changed the same key to different values) resolve in
favour of this run, and are reported on stderr so they show up in the job
log. In practice they only happen on the shared company-name cache, where
either value is equally correct.

Usage:
    python merge_state.py --base base.json --theirs theirs.json \\
                          --ours state.json --out state.json
"""

import argparse
import json
import sys


def load(path, label):
    """Missing or unparseable inputs are treated as empty rather than fatal.

    A first run has no base, and a corrupt blob should not strand the bot --
    the merge still produces something sane, and the log says what happened.
    """
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"merge_state: {label} missing ({path}); treating as empty.",
              file=sys.stderr)
        return {}
    except (ValueError, OSError) as e:
        print(f"merge_state: {label} unreadable ({path}): {e}; treating as empty.",
              file=sys.stderr)
        return {}
    if not isinstance(data, dict):
        print(f"merge_state: {label} is not a JSON object; treating as empty.",
              file=sys.stderr)
        return {}
    return data


def merge(base: dict, ours: dict, theirs: dict):
    """Return (merged, report). Applies our changes-since-base onto theirs."""
    merged = dict(theirs)

    added = changed = deleted = conflicts = 0

    # Keys we created or modified.
    for key, value in ours.items():
        if key not in base:
            if key not in theirs:
                added += 1
            elif theirs[key] != value:
                conflicts += 1
                print(f"merge_state: conflict on new key {key!r}; keeping ours.",
                      file=sys.stderr)
            merged[key] = value
        elif base[key] != value:
            if key in theirs and theirs[key] != base[key] and theirs[key] != value:
                conflicts += 1
                print(f"merge_state: conflict on {key!r}; keeping ours.",
                      file=sys.stderr)
            merged[key] = value
            changed += 1
        # else: unchanged by us -- leave whatever theirs has, which may be
        # newer. This is the case that preserves the other workflow's work.

    # Keys we deliberately removed. Only honour a deletion when the other
    # side did not meanwhile change the value: if they did, they know
    # something we don't, and reviving the key is the safer error.
    for key in base:
        if key in ours:
            continue
        if key in theirs and theirs[key] != base[key]:
            print(f"merge_state: {key!r} deleted by us but changed by them; "
                  f"keeping theirs.", file=sys.stderr)
            continue
        merged.pop(key, None)
        deleted += 1

    report = {"added": added, "changed": changed,
              "deleted": deleted, "conflicts": conflicts}
    return merged, report


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", required=True,
                    help="state.json as of the commit this run checked out")
    ap.add_argument("--ours", required=True, help="state.json this run produced")
    ap.add_argument("--theirs", required=True, help="state.json on origin/main")
    ap.add_argument("--out", required=True, help="where to write the merge")
    args = ap.parse_args()

    base = load(args.base, "base")
    ours = load(args.ours, "ours")
    theirs = load(args.theirs, "theirs")

    merged, report = merge(base, ours, theirs)

    with open(args.out, "w") as f:
        json.dump(merged, f, indent=2, sort_keys=True)
        f.write("\n")

    print(f"merge_state: {report['added']} added, {report['changed']} changed, "
          f"{report['deleted']} deleted, {report['conflicts']} conflicts; "
          f"{len(merged)} keys written.")


if __name__ == "__main__":
    main()
