"""Polls Telegram for new messages and processes natural-language commands
for managing your holdings list (tickers.json), a separate no-ownership
watchlist (watchlist.json), your position tracking (holdings.json), and
on-demand earnings lookups.

Recognized commands (case-insensitive, a leading "$" on a ticker is
optional):
  add <TICKER> to my list
  remove <TICKER> from my list
  add <TICKER>[, <TICKER> ...] to my watchlist
  remove <TICKER>[, <TICKER> ...] from my watchlist
  watchlist
  added <NUMBER> shares of <TICKER> at <PRICE>
  sold <NUMBER> shares of <TICKER> at <PRICE>
  summary
  earnings today
  earnings for <TICKER>[, <TICKER> ...]

"add ... to my watchlist" (comma/"and"-separated list of tickers accepted)
adds symbols to watchlist.json -- a separate list from tickers.json/"my
list", for stocks you want price and news alerts on WITHOUT owning them.
Each symbol is validated the same way as "add TICKER to my list" (must
have live market data). "remove ... from my watchlist" removes them.
"watchlist" sends every symbol on that list with its current price and %
move vs. yesterday's close. Watchlist symbols get the exact same 5%
price-move alerting and material-news alerting as your holdings (see
monitor.py), via the same cron-job.org-triggered polling -- they're just
excluded from position tracking (holdings.json) and per-holding earnings
reminders (earnings_watch.py), since those are specifically about things
you own.
"added ... at ..." recalculates your weighted-average book price per share
for that ticker (existing shares/cost blended with the new lot), and adds
the ticker to your watchlist automatically if it isn't already there.
"sold ... at ..." reduces your share count -- the average cost per
remaining share is left unchanged, which is standard average-cost-basis
accounting (selling doesn't change what you paid for what's left) -- and
requires the sale price so it can track two running totals stored directly
in holdings.json: CASH (total proceeds from all sales, i.e. qty * sale
price, added up across every "sold" command) and REALIZED_PNL (total
realized gain/loss across all sales, i.e. sum of qty * (sale price - avg
cost at the time of each sale)). Both are portfolio-wide running totals,
not per-ticker. The old "sold <NUMBER> shares of <TICKER>" form (no price)
is no longer enough to record a sale -- you'll get a reminder to include
the price instead.
"summary" sends your current holdings: shares, avg book price, total book
value, live market price, and % upside/downside per position, plus a
portfolio total, plus your running cash-from-sales balance and realized
P&L from sales (if either is non-zero).
"earnings today" immediately sends the day's top market-cap and
most-analyst-attention earnings reporters -- there's no more automatic
daily 3:55pm send, this is fully on-demand now.
"earnings for <TICKER, TICKER, ...>" first checks each symbol against
Finnhub's earnings calendar for today -- any ticker NOT reporting today
gets an immediate reply saying so instead of being queued (so you don't
wait hours only to be told "still not detected"). Whatever's left gets
queued to be polled starting at MARKET_EARNINGS_POLL_START_ET ET that
same day (or immediately, if that time has already passed) -- a
beat/miss summary is sent as soon as each release is detected, using the
same detection method as earnings_watch.py. Polling continues across
runs of this same 5-minute cron job (see check_on_demand_earnings
below), so it keeps checking even if you don't text anything else. If
the Finnhub calendar fetch itself fails or FINNHUB_API_KEY isn't set
(data source hiccup), we can't confidently rule anything out, so
everything gets queued as before rather than risk a false "not
reporting" reply.

Runs on a schedule via .github/workflows/telegram_commands.yml (every ~5
min). GitHub Actions cron isn't guaranteed to fire exactly on time -- it
can lag by several minutes, more on busy days -- so there can be a real
delay between texting the bot and getting a reply. You can also run it
manually via workflow_dispatch for an immediate check.

Any message that doesn't match one of the patterns above is ignored (no
reply), so normal chatter with the bot doesn't trigger anything.
"""

import json
import os
import re
from datetime import timedelta

import requests
import yfinance as yf

from config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    MARKET_EARNINGS_POLL_START_ET,
    EARNINGS_POLL_TIMEOUT_MINUTES,
)
from telegram_utils import send_telegram_message
from state_utils import load_state, save_state
from earnings_utils import now_et, date_str_et, fetch_earnings_calendar_finnhub
from earnings_summary import get_earnings_release, build_summary_message
from market_earnings_watch import select_top_reporters, format_list_line

TICKERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tickers.json")
HOLDINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "holdings.json")
WATCHLIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist.json")

# Portfolio-wide running totals stored as top-level scalar entries in
# holdings.json, alongside the per-ticker {"shares", "avg_cost"} dicts.
# Reserved -- can't be used as ticker symbols in add/sold commands.
CASH_KEY = "CASH"
REALIZED_PNL_KEY = "REALIZED_PNL"
RESERVED_HOLDINGS_KEYS = {CASH_KEY, REALIZED_PNL_KEY}

_NUM = r"[\d,]+(?:\.\d+)?"
_TICKER = r"\$?([A-Za-z.\-]{1,10})"

ADD_RE = re.compile(rf"^\s*add\s+{_TICKER}\s+to\s+my\s+list\.?\s*$", re.IGNORECASE)
REMOVE_RE = re.compile(rf"^\s*remove\s+{_TICKER}\s+from\s+my\s+list\.?\s*$", re.IGNORECASE)
ADD_SHARES_RE = re.compile(
    rf"^\s*add(?:ed)?\s+({_NUM})\s+shares?\s+of\s+{_TICKER}\s+at\s+\$?({_NUM})\s*\.?\s*$",
    re.IGNORECASE,
)
SOLD_SHARES_RE = re.compile(
    rf"^\s*sold\s+({_NUM})\s+shares?\s+of\s+{_TICKER}\s+at\s+\$?({_NUM})\s*\.?\s*$", re.IGNORECASE
)
# Matches the old no-price form, purely to catch it and tell the user a
# price is now required (rather than silently ignoring the message).
SOLD_SHARES_NO_PRICE_RE = re.compile(
    rf"^\s*sold\s+({_NUM})\s+shares?\s+of\s+{_TICKER}\s*\.?\s*$", re.IGNORECASE
)
ADD_WATCHLIST_RE = re.compile(r"^\s*add\s+(.+?)\s+to\s+my\s+watchlist\.?\s*$", re.IGNORECASE)
REMOVE_WATCHLIST_RE = re.compile(r"^\s*remove\s+(.+?)\s+from\s+my\s+watchlist\.?\s*$", re.IGNORECASE)
WATCHLIST_RE = re.compile(r"^\s*watchlist\.?\s*$", re.IGNORECASE)
SUMMARY_RE = re.compile(r"^\s*summary\s*\.?\s*$", re.IGNORECASE)
EARNINGS_TODAY_RE = re.compile(r"^\s*earnings\s+today\.?\s*$", re.IGNORECASE)
EARNINGS_FOR_RE = re.compile(r"^\s*earnings\s+for\s+(.+?)\.?\s*$", re.IGNORECASE)


def parse_num(s: str) -> float:
    return float(s.replace(",", ""))


def parse_ticker_list(raw: str) -> list[str]:
    """Splits a free-form ticker list on commas and/or "and" -- handles
    "AAPL, MSFT", "AAPL and MSFT", "AAPL, MSFT and GOOGL", etc."""
    parts = re.split(r",|\band\b", raw, flags=re.IGNORECASE)
    seen = set()
    result = []
    for part in parts:
        t = part.strip().strip(".").lstrip("$").upper()
        if t and t not in seen:
            seen.add(t)
            result.append(t)
    return result


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


def load_watchlist() -> list[str]:
    try:
        with open(WATCHLIST_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def save_watchlist(watchlist: list[str]) -> None:
    with open(WATCHLIST_FILE, "w") as f:
        json.dump(watchlist, f, indent=2)
        f.write("\n")


def load_holdings() -> dict:
    try:
        with open(HOLDINGS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_holdings(holdings: dict) -> None:
    with open(HOLDINGS_FILE, "w") as f:
        json.dump(holdings, f, indent=2)
        f.write("\n")


def validate_ticker(ticker: str) -> bool:
    """Quick sanity check that yfinance actually has live data for this
    symbol, so a typo doesn't silently get added to your holdings."""
    try:
        info = yf.Ticker(ticker).fast_info
        return info["last_price"] is not None
    except Exception:
        return False


def get_current_price(ticker: str) -> float | None:
    try:
        info = yf.Ticker(ticker).fast_info
        return info["last_price"]
    except Exception:
        return None


def format_usd(x: float) -> str:
    return f"${x:,.2f}"


def format_pct_signed(x: float) -> str:
    return f"{x:+.1f}%"


def format_usd_signed(x: float) -> str:
    sign = "+" if x >= 0 else "-"
    return f"{sign}${abs(x):,.2f}"


def get_updates(offset: int | None) -> list[dict]:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": 0}
    if offset is not None:
        params["offset"] = offset
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    return resp.json().get("result", [])


def send_summary(holdings: dict) -> None:
    cash = holdings.get(CASH_KEY, 0.0)
    realized_pnl = holdings.get(REALIZED_PNL_KEY, 0.0)
    tickers_held = sorted(
        t
        for t, pos in holdings.items()
        if t not in RESERVED_HOLDINGS_KEYS and isinstance(pos, dict) and pos.get("shares", 0) > 0
    )
    if not tickers_held and not cash and not realized_pnl:
        send_telegram_message("You don't have any tracked positions yet.")
        return

    lines = ["\U0001F4CA *Portfolio Summary*"]
    total_book = 0.0
    total_market = 0.0
    have_market_total = True

    for ticker in tickers_held:
        pos = holdings[ticker]
        shares = pos["shares"]
        avg_cost = pos["avg_cost"]
        book_value = shares * avg_cost
        total_book += book_value

        price = get_current_price(ticker)
        lines.append("")
        lines.append(f"*{ticker}*:")
        lines.append(f"{shares:,.0f} shares @ avg {format_usd(avg_cost)}")
        if price is None:
            lines.append("Current price: unavailable")
            lines.append(f"Book value: {format_usd(book_value)}")
            have_market_total = False
        else:
            market_value = shares * price
            total_market += market_value
            pct = (price - avg_cost) / avg_cost * 100 if avg_cost else 0.0
            arrow = "\U0001F7E2" if pct >= 0 else "\U0001F534"
            lines.append(f"Current: {format_usd(price)}/share ({format_pct_signed(pct)}) {arrow}")
            diff = market_value - book_value
            lines.append(f"Book value: {format_usd(book_value)} ({format_usd_signed(diff)})")

    lines.append("")
    if have_market_total and total_book:
        total_pct = (total_market - total_book) / total_book * 100
        lines.append(
            f"*Total*: book {format_usd(total_book)}  |  market {format_usd(total_market)}  |  "
            f"{format_pct_signed(total_pct)}"
        )
    else:
        lines.append(f"*Total book value*: {format_usd(total_book)}")

    if cash:
        lines.append(f"*Cash from sales*: {format_usd(cash)}")
    if realized_pnl:
        arrow = "\U0001F7E2" if realized_pnl >= 0 else "\U0001F534"
        lines.append(f"*Realized P&L (sales)*: {format_usd_signed(realized_pnl)} {arrow}")
    if cash and have_market_total:
        lines.append(f"*Portfolio value (stocks + cash)*: {format_usd(total_market + cash)}")

    send_telegram_message("\n".join(lines))


def handle_earnings_today() -> None:
    today = date_str_et(0)
    top_cap, top_analyst = select_top_reporters(today)
    if not top_cap and not top_analyst:
        send_telegram_message(f"No market-wide earnings calendar data for today ({today}).")
        return

    lines = [f"\U0001F4CB *Top {len(top_cap)} market-wide earnings today* ({today}):"]
    lines += [format_list_line(r) for r in top_cap] if top_cap else ["None found."]
    send_telegram_message("\n".join(lines))

    lines2 = ["\U0001F4CB *Most analyst attention today*:"]
    lines2 += [format_list_line(r) for r in top_analyst] if top_analyst else ["None found."]
    send_telegram_message("\n".join(lines2))


def handle_earnings_for(raw: str, state: dict) -> None:
    requested = parse_ticker_list(raw)
    if not requested:
        send_telegram_message('Couldn\'t parse any tickers from that -- try "earnings for AAPL, MSFT".')
        return

    valid = [t for t in requested if validate_ticker(t)]
    invalid = [t for t in requested if t not in valid]
    if invalid:
        send_telegram_message(
            f"Couldn't find market data for: {', '.join(invalid)} -- double-check the symbol(s)."
        )
    if not valid:
        return

    today = date_str_et(0)

    # Check today's Finnhub earnings calendar before queuing anything, so a
    # ticker that isn't reporting today gets told immediately instead of
    # sitting in a 3-hour poll that's doomed to end in a "still not
    # detected" message. If the calendar fetch itself fails/comes back
    # empty (data-source hiccup, or FINNHUB_API_KEY not set), we can't
    # confidently rule anything out, so skip this check and queue
    # everything as before.
    calendar_rows = fetch_earnings_calendar_finnhub(today)
    if calendar_rows:
        reporting_today = {row.get("symbol") for row in calendar_rows if row.get("symbol")}
        not_reporting = [t for t in valid if t not in reporting_today]
        to_queue = [t for t in valid if t in reporting_today]
    else:
        not_reporting = []
        to_queue = valid

    if not_reporting:
        verb = "isn't" if len(not_reporting) == 1 else "aren't"
        send_telegram_message(
            f"Per Finnhub's calendar, {', '.join(not_reporting)} {verb} reporting earnings "
            f"today ({today}), so I won't poll for {'it' if len(not_reporting) == 1 else 'them'}."
        )

    if not to_queue:
        return

    key = f"ew_on_demand::{today}"
    existing = set(state.get(key, []))
    existing.update(to_queue)
    state[key] = sorted(existing)

    send_telegram_message(
        f"\U0001F514 Got it -- I'll start checking for earnings from {', '.join(to_queue)} "
        f"starting at {MARKET_EARNINGS_POLL_START_ET} ET today, and text you a summary as soon "
        f"as each is released."
    )


def handle_watchlist(watchlist: list[str]) -> None:
    if not watchlist:
        send_telegram_message(
            'Your watchlist is empty. Add tickers with "add TICKER, TICKER to my watchlist".'
        )
        return

    lines = ["\U0001F440 *Watchlist*"]
    for ticker in sorted(watchlist):
        try:
            info = yf.Ticker(ticker).fast_info
            price = info["last_price"]
            prev_close = info["previous_close"]
        except Exception:
            price = None
            prev_close = None

        if price is not None and prev_close:
            pct = (price - prev_close) / prev_close * 100
            arrow = "\U0001F7E2" if pct >= 0 else "\U0001F534"
            lines.append(f"*{ticker}*: {format_usd(price)} ({format_pct_signed(pct)} vs prev close) {arrow}")
        else:
            lines.append(f"*{ticker}*: price unavailable")

    send_telegram_message("\n".join(lines))


def check_on_demand_earnings(state: dict) -> bool:
    """Called on every run. Once it's on/after MARKET_EARNINGS_POLL_START_ET
    ET, checks any tickers requested today via "earnings for ..." once per
    run -- sending a summary the moment a release is detected, and a
    one-time give-up notice after EARNINGS_POLL_TIMEOUT_MINUTES past the
    poll-start time. Returns whether state was mutated."""
    now = now_et()
    today = date_str_et(0)
    hh, mm = (int(p) for p in MARKET_EARNINGS_POLL_START_ET.split(":"))
    poll_start = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if now < poll_start:
        return False

    key = f"ew_on_demand::{today}"
    requested = state.get(key, [])
    if not requested:
        return False

    deadline = poll_start + timedelta(minutes=EARNINGS_POLL_TIMEOUT_MINUTES)
    changed = False
    for ticker in requested:
        sent_key = f"ew_summary_sent::{ticker}::{today}"
        giveup_key = f"ew_on_demand_giveup::{ticker}::{today}"
        if state.get(sent_key) or state.get(giveup_key):
            continue
        data = get_earnings_release(ticker, today)
        if data:
            send_telegram_message(build_summary_message(ticker, today, data))
            state[sent_key] = True
            changed = True
        elif now >= deadline:
            send_telegram_message(
                f"⚠️ *{ticker}*: earnings still not detected as released after "
                f"~{EARNINGS_POLL_TIMEOUT_MINUTES} min of checking. It may be delayed -- worth a manual look."
            )
            state[giveup_key] = True
            changed = True

    return changed


def process_message(
    text: str, tickers: list[str], holdings: dict, watchlist: list[str], state: dict
) -> tuple[bool, bool, bool]:
    """Handles one message's text, mutating `tickers`, `holdings`, and/or
    `watchlist` in place and sending a Telegram reply. Returns
    (tickers_changed, holdings_changed, watchlist_changed)."""

    if SUMMARY_RE.match(text):
        send_summary(holdings)
        return False, False, False

    if WATCHLIST_RE.match(text):
        handle_watchlist(watchlist)
        return False, False, False

    if EARNINGS_TODAY_RE.match(text):
        handle_earnings_today()
        return False, False, False

    earnings_for_match = EARNINGS_FOR_RE.match(text)
    if earnings_for_match:
        handle_earnings_for(earnings_for_match.group(1), state)
        return False, False, False

    add_shares_match = ADD_SHARES_RE.match(text)
    if add_shares_match:
        qty = parse_num(add_shares_match.group(1))
        ticker = add_shares_match.group(2).upper()
        price = parse_num(add_shares_match.group(3))

        if ticker in RESERVED_HOLDINGS_KEYS:
            send_telegram_message(f"*{ticker}* is a reserved name and can't be used as a ticker symbol.")
            return False, False, False

        tickers_changed = False
        if ticker not in tickers:
            if not validate_ticker(ticker):
                send_telegram_message(
                    f"Couldn't find market data for *{ticker}* -- double-check the symbol and try again."
                )
                return False, False, False
            tickers.append(ticker)
            tickers_changed = True

        pos = holdings.get(ticker, {"shares": 0.0, "avg_cost": 0.0})
        old_shares = pos.get("shares", 0.0)
        old_cost_total = old_shares * pos.get("avg_cost", 0.0)
        new_shares = old_shares + qty
        new_cost_total = old_cost_total + qty * price
        new_avg = new_cost_total / new_shares if new_shares else 0.0
        holdings[ticker] = {"shares": new_shares, "avg_cost": new_avg}

        watch_note = " Also added it to your watchlist for price/news/earnings alerts." if tickers_changed else ""
        send_telegram_message(
            f"✅ Added {qty:,.0f} shares of *{ticker}* at {format_usd(price)}.\n"
            f"New position: {new_shares:,.0f} sh @ avg {format_usd(new_avg)} "
            f"(book {format_usd(new_shares * new_avg)}).{watch_note}"
        )
        return tickers_changed, True, False

    sold_match = SOLD_SHARES_RE.match(text)
    if sold_match:
        qty = parse_num(sold_match.group(1))
        ticker = sold_match.group(2).upper()
        price = parse_num(sold_match.group(3))

        if ticker in RESERVED_HOLDINGS_KEYS:
            send_telegram_message(f"*{ticker}* is a reserved name and can't be used as a ticker symbol.")
            return False, False, False

        pos = holdings.get(ticker)
        if not isinstance(pos, dict) or pos.get("shares", 0) <= 0:
            send_telegram_message(f"You don't have a tracked position in *{ticker}* to sell from.")
            return False, False, False
        if qty > pos["shares"] + 1e-9:
            send_telegram_message(
                f"You only have {pos['shares']:,.0f} shares of *{ticker}* on record -- can't sell {qty:,.0f}."
            )
            return False, False, False

        # Standard average-cost-basis accounting: selling doesn't change the
        # avg_cost of what's left. Proceeds and realized gain/loss (vs. that
        # avg_cost) roll into two portfolio-wide running totals in
        # holdings.json rather than per-ticker fields.
        avg_cost = pos["avg_cost"]
        proceeds = qty * price
        realized_gain = qty * (price - avg_cost)

        new_shares = pos["shares"] - qty
        if new_shares < 1e-9:
            new_shares = 0.0
        holdings[ticker]["shares"] = new_shares
        holdings[CASH_KEY] = holdings.get(CASH_KEY, 0.0) + proceeds
        holdings[REALIZED_PNL_KEY] = holdings.get(REALIZED_PNL_KEY, 0.0) + realized_gain

        book_value = new_shares * avg_cost
        gain_word = "gain" if realized_gain >= 0 else "loss"
        send_telegram_message(
            f"\U0001F5D1 Sold {qty:,.0f} shares of *{ticker}* at {format_usd(price)}.\n"
            f"Proceeds: {format_usd(proceeds)}  |  Realized {gain_word}: {format_usd_signed(realized_gain)}\n"
            f"Remaining: {new_shares:,.0f} sh @ avg {format_usd(avg_cost)} (book {format_usd(book_value)}).\n"
            f"Cash balance: {format_usd(holdings[CASH_KEY])}  |  "
            f"Total realized P&L: {format_usd_signed(holdings[REALIZED_PNL_KEY])}"
        )
        return False, True, False

    sold_no_price_match = SOLD_SHARES_NO_PRICE_RE.match(text)
    if sold_no_price_match:
        send_telegram_message(
            'To track cash and realized P&L I need the sale price -- try '
            '"sold 100 shares of AAPL at 150".'
        )
        return False, False, False

    add_match = ADD_RE.match(text)
    if add_match:
        ticker = add_match.group(1).upper()
        if ticker in tickers:
            send_telegram_message(f"*{ticker}* is already on your list.")
            return False, False, False
        if not validate_ticker(ticker):
            send_telegram_message(
                f"Couldn't find market data for *{ticker}* -- double-check the symbol and try again."
            )
            return False, False, False
        tickers.append(ticker)
        send_telegram_message(
            f"✅ Added *{ticker}* to your holdings. You'll now get price/news "
            f"alerts and earnings reminders for it, same as your other tickers."
        )
        return True, False, False

    remove_match = REMOVE_RE.match(text)
    if remove_match:
        ticker = remove_match.group(1).upper()
        if ticker not in tickers:
            send_telegram_message(f"*{ticker}* isn't on your list.")
            return False, False, False
        tickers.remove(ticker)
        send_telegram_message(f"\U0001F5D1 Removed *{ticker}* from your watchlist.")
        return True, False, False

    add_watchlist_match = ADD_WATCHLIST_RE.match(text)
    if add_watchlist_match:
        requested = parse_ticker_list(add_watchlist_match.group(1))
        if not requested:
            send_telegram_message(
                'Couldn\'t parse any tickers from that -- try "add AAPL, MSFT to my watchlist".'
            )
            return False, False, False

        reserved = [t for t in requested if t in RESERVED_HOLDINGS_KEYS]
        candidates = [t for t in requested if t not in RESERVED_HOLDINGS_KEYS]
        already = [t for t in candidates if t in watchlist]
        new_candidates = [t for t in candidates if t not in watchlist]

        valid = [t for t in new_candidates if validate_ticker(t)]
        invalid = [t for t in new_candidates if t not in valid]

        for t in valid:
            watchlist.append(t)

        if reserved:
            send_telegram_message(f"Reserved, can't be used as ticker symbols: {', '.join(reserved)}.")
        if invalid:
            send_telegram_message(
                f"Couldn't find market data for: {', '.join(invalid)} -- double-check the symbol(s)."
            )
        if already:
            send_telegram_message(f"Already on your watchlist: {', '.join(already)}.")
        if valid:
            send_telegram_message(
                f"\U0001F440 Added {', '.join(valid)} to your watchlist -- you'll get 5% price-move "
                f"and material-news alerts for {'it' if len(valid) == 1 else 'them'}, same as your holdings."
            )
        return False, False, bool(valid)

    remove_watchlist_match = REMOVE_WATCHLIST_RE.match(text)
    if remove_watchlist_match:
        requested = parse_ticker_list(remove_watchlist_match.group(1))
        if not requested:
            send_telegram_message(
                'Couldn\'t parse any tickers from that -- try "remove AAPL, MSFT from my watchlist".'
            )
            return False, False, False

        present = [t for t in requested if t in watchlist]
        missing = [t for t in requested if t not in watchlist]

        for t in present:
            watchlist.remove(t)

        if missing:
            send_telegram_message(f"Not on your watchlist: {', '.join(missing)}.")
        if present:
            send_telegram_message(f"\U0001F5D1 Removed {', '.join(present)} from your watchlist.")
        return False, False, bool(present)

    return False, False, False


def main() -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; skipping.")
        return

    state = load_state()
    offset = state.get("tg_update_offset")
    tickers = load_tickers()
    holdings = load_holdings()
    watchlist = load_watchlist()
    tickers_changed = False
    holdings_changed = False
    watchlist_changed = False

    updates = get_updates(offset)
    if updates:
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
            t_changed, h_changed, w_changed = process_message(text, tickers, holdings, watchlist, state)
            tickers_changed = tickers_changed or t_changed
            holdings_changed = holdings_changed or h_changed
            watchlist_changed = watchlist_changed or w_changed
    else:
        print("No new Telegram messages.")

    # Runs every invocation (not just when there's a new Telegram message)
    # so on-demand earnings polling keeps going in the background.
    check_on_demand_earnings(state)

    if tickers_changed:
        save_tickers(tickers)
        print(f"tickers.json updated: {tickers}")
    if holdings_changed:
        save_holdings(holdings)
        print(f"holdings.json updated: {holdings}")
    if watchlist_changed:
        save_watchlist(watchlist)
        print(f"watchlist.json updated: {watchlist}")

    save_state(state)


if __name__ == "__main__":
    main()
