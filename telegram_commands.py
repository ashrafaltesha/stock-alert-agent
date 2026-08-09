"""Polls Telegram for new messages and processes natural-language commands
to add or remove tickers from your holdings list (tickers.json).

Recognized commands (case-insensitive, a leading "$" on the ticker is
optional):
  add <TICKER> to my list
  remove <TICKER> from my list

Runs on a schedule via .github/workflows/telegram_commands.yml (every ~5
min). GitHub Actions cron isn't guaranteed to fire exactly on time -- it
can lag by several minutes, more on busy days -- so there can be a real
delay between texting the bot and getting a confirmation back, or seeing
the ticker picked up by monitor.py / the earnings watchers. You can also
run it manually via workflow_dispatch for an immediate check.

Any message that doesn't match one of the two patterns above is ignored
(no reply), so normal chatter with the bot doesn't trigger anything.
"""

import json
import os
import re

import requests
import yfinance as yf

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from telegram_utils import send_telegram_message
from state_utils import load_state, save_state

TICKERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tickers.json")

ADD_RE = re.compile(r"^\s*add\s+\$?([A-Za-z.\-]{1,10})\s+to\s+my\s+list\.?\s*$", re.IGNORECASE)
REMOVE_RE = re.compile(r"^\s*remove\s+\$?([A-Za-z.\-]{1,10})\s+from\s+my\s+list\.?\s*$", re.IGNORECASE)


def load_tickers() -> list[str]:
    try:
        with open(TICKERS_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def save_tickers(tickers: list[str]) -> None:
    with open(TICKERS_FILE, "w") as f:
        json.dump(tickers, f, indent=2)
        f.write("\n")


def validate_ticker(ticker: str) -> bool:
    """Quick sanity check that yfinance actually has live data for this
    symbol, so a typo doesn't silently get added to your holdings."""
    try:
        info = yf.Ticker(ticker).fast_info
        return info["last_price"] is not None
    except Exception:
        return False


def get_updates(offset: int | None) -> list[dict]:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": 0}
    if offset is not None:
        params["offset"] = offset
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    return resp.json().get("result", [])


def process_message(text: str, tickers: list[str]) -> bool:
    """Handles one message's text against the add/remove patterns, mutating
    `tickers` in place and sending a Telegram confirmation. Returns True if
    tickers.json needs to be saved."""
    add_match = ADD_RE.match(text)
    remove_match = REMOVE_RE.match(text)

    if add_match:
        ticker = add_match.group(1).upper()
        if ticker in tickers:
            send_telegram_message(f"*{ticker}* is already on your list.")
            return False
        if not validate_ticker(ticker):
            send_telegram_message(
                f"Couldn't find market data for *{ticker}* -- double-check the symbol and try again."
            )
            return False
        tickers.append(ticker)
        send_telegram_message(
            f"✅ Added *{ticker}* to your holdings. You'll now get price/news "
            f"alerts and earnings reminders for it, same as your other tickers."
        )
        return True

    if remove_match:
        ticker = remove_match.group(1).upper()
        if ticker not in tickers:
            send_telegram_message(f"*{ticker}* isn't on your list.")
            return False
        tickers.remove(ticker)
        send_telegram_message(f"\U0001F5D1 Removed *{ticker}* from your holdings.")
        return True

    return False


def main() -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; skipping.")
        return

    state = load_state()
    offset = state.get("tg_update_offset")

    updates = get_updates(offset)
    if not updates:
        print("No new Telegram messages.")
        return

    tickers = load_tickers()
    changed = False

    for update in updates:
        state["tg_update_offset"] = update["update_id"] + 1

        message = update.get("message") or update.get("edited_message")
        if not message:
            continue

        chat_id = str(message.get("chat", {}).get("id", ""))
        if str(TELEGRAM_CHAT_ID) != chat_id:
            print(f"Ignoring message from unexpected chat_id={chat_id}")
            continue

        text = message.get("text", "")
        if process_message(text, tickers):
            changed = True

    if changed:
        save_tickers(tickers)
        print(f"tickers.json updated: {tickers}")

    save_state(state)


if __name__ == "__main__":
    main()
