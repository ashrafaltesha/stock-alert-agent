"""Shared state.json load/save, used by every script that needs to
remember things across runs (monitor.py, earnings_watch.py,
market_earnings_watch.py)."""

import json
import os

from config import STATE_FILE


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)
