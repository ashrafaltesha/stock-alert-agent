"""Shared state.json load/save, used by every script that needs to remember
things across runs (monitor.py, earnings_watch.py, telegram_commands.py)."""

import json
import os

from config import STATE_FILE

# Key families written by code that no longer exists.
#
# These are not merely clutter. During the 2026-08-24 outage I read
# state.json repeatedly looking for `ew_watch::XPEV`, and `ew_on_demand::`,
# `ew_summary_sent::` and `ir_page::` sat next to it looking equally current.
# Ruling them out cost time in the middle of an incident, which is the worst
# possible moment to be reverse-engineering which half of a file is alive.
#
# The lesson, recorded because it will apply again: a state key outlives the
# code that wrote it unless something deletes it. Retire the key in the same
# change that retires the feature, and add the prefix here.
RETIRED_PREFIXES = (
    "ir_page::",              # the IR-page scraper, replaced by SEC filings
    "ew_amc_reminder_sent::",  # the BMO/AMC reminder jobs
    "ew_poll_started::",       # the windowed premarket/afterhours watcher
    "ew_summary_sent::",       # the Finnhub-actuals summary path
    "ew_on_demand::",          # the pre-SEC on-demand watch
    "ew_on_demand_giveup::",
)


def prune_retired(state: dict) -> int:
    """Drop keys belonging to deleted features. Returns how many went.

    Runs on load, so it needs no migration script and no one-off workflow --
    the next run that saves state persists the smaller file.
    """
    dead = [k for k in state if k.startswith(RETIRED_PREFIXES)]
    for key in dead:
        del state[key]
    if dead:
        print(f"Pruned {len(dead)} retired state key(s): "
              f"{', '.join(sorted(dead)[:6])}"
              f"{' ...' if len(dead) > 6 else ''}")
    return len(dead)


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
        prune_retired(state)
        return state
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)
