"""Listens for Telegram messages and processes natural-language commands
for managing your holdings list (tickers.json), a separate no-ownership
watchlist (watchlist.json), your position tracking (holdings.json), and
on-demand earnings lookups.

Two modes:
  `python telegram_commands.py listen`  the loop the workflow runs. Holds an
      open long-poll connection to Telegram for 62 minutes and replies within
      a second or two of a message arriving.
  `python telegram_commands.py`         one-shot: drain whatever is waiting,
      then exit. Kept for manual runs and as a fallback.

Recognized commands (case-insensitive, a leading "$" on a ticker is
optional):
  add <TICKER> to my list
  remove <TICKER> from my list
  add <TICKER>[, <TICKER> ...] to my watchlist
  remove <TICKER>[, <TICKER> ...] from my watchlist
  watchlist
  added <NUMBER> shares of <TICKER> at <PRICE>
  sold <NUMBER> shares of <TICKER> at <PRICE>
  set cash to <AMOUNT>
  set deposits to <AMOUNT>   (aka "set money in to ...")
  deposited <AMOUNT>
  withdrew <AMOUNT>
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
requires the sale price so it can track the portfolio-wide running totals
stored directly in holdings.json: CASH (money on hand), REALIZED_PNL (total
gain/loss across all sales) and DEPOSITS (net money you have put in).

CASH is a real balance, not just sale proceeds: buying deducts, selling
credits, and "deposited"/"withdrew" adjust it directly. If a purchase costs
more than the cash on hand, the shortfall is assumed to have been deposited
rather than letting the balance go negative -- the money had to come from
somewhere for the trade to have happened -- and that implied amount is added
to DEPOSITS so the balance still reconciles.

DEPOSITS exists so returns mean something. Book value ignores money added
along the way, so "am I up?" can only be answered against what you actually
put in. The first "set cash to X" seeds it with X plus the cost of whatever
you already hold, since those shares were paid for before the bot existed;
"set deposits to X" corrects it. The old "sold <NUMBER> shares of <TICKER>" form (no price)
is no longer enough to record a sale -- you'll get a reminder to include
the price instead.
"summary" sends your current holdings: shares, avg book price, total book
value, live market price, and % upside/downside per position, plus a
portfolio total, plus your running cash-from-sales balance and realized
P&L from sales (if either is non-zero).
"earnings today" immediately sends the day's top market-cap and
most-analyst-attention earnings reporters -- there's no more automatic
daily 3:55pm send, this is fully on-demand now.
"earnings for <TICKER, TICKER, ...>" arms a 24-hour watch on each symbol.
Any ticker works, held or not. Detection itself lives in earnings_watch.py,
which polls SEC filings every 15 seconds in a long-running job -- domestic
8-K item 2.02 (the SEC's own label for a results release) and foreign 6-K
scored on financial content. Results reach you within seconds of filing.

Finnhub is consulted for the earnings *calendar* only, and its answer is
advisory: if it doesn't list a ticker as reporting today you get a note
saying so, but the watch is armed regardless. Its calendar has been wrong
often enough -- especially on recent IPOs and foreign issuers -- that letting
it veto a watch would be worse than the occasional wasted one.

Runs on a schedule via .github/workflows/telegram_commands.yml (every ~1
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
import copy
import subprocess
import sys
import time
from datetime import datetime, timedelta

import requests
import yfinance as yf

import health
import heartbeat

# Telegram holds the connection open this long when there is nothing to
# report. 25s is comfortably inside Telegram's own limit and keeps an idle
# hour to ~144 requests.
POLL_TIMEOUT_SECONDS = 25

# Five and a half hours, not 62 minutes, against an hourly cron.
#
# The original reasoning -- "loop slightly longer than the restart interval
# so runs overlap" -- assumed the restart interval was real. It is not.
# GitHub delivers scheduled events best-effort, and on 2026-08-24 this
# workflow's hourly cron actually fired at 19:56, 21:03, 23:33, 01:58, 04:29,
# 06:58 and 10:26: gaps of up to 89 minutes. A 62-minute loop against a
# 150-minute interval means the bot is simply not listening for an hour and
# a half at a time, which is exactly how it looked from the outside.
#
# A long loop inverts the dependency. The cron is now an opportunity to
# refresh rather than a lifeline: whenever it fires, cancel-in-progress
# replaces the running listener with a newer one, and when it does not fire,
# the existing listener just keeps going. GitHub caps a job at six hours.
LOOP_MINUTES = 330

# A long loop runs stale code until it ends. Checked every N long-poll cycles
# (25s each), so roughly every ten minutes.
UPDATE_CHECK_EVERY_N_CYCLES = 24

from config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    ON_DEMAND_WATCH_HOURS,
)
from telegram_utils import send_telegram_message, escape_markdown
from state_utils import load_state, save_state
from earnings_utils import (
    now_et,
    date_str_et,
    fetch_earnings_calendar_finnhub,
    arm_earnings_watch,
)
from market_earnings_watch import select_top_reporters, format_list_line

TICKERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tickers.json")
HOLDINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "holdings.json")
WATCHLIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist.json")

# Portfolio-wide running totals stored as top-level scalar entries in
# holdings.json, alongside the per-ticker {"shares", "avg_cost"} dicts.
# Reserved -- can't be used as ticker symbols in add/sold commands.
CASH_KEY = "CASH"
REALIZED_PNL_KEY = "REALIZED_PNL"
# Running total of money added to the account, including the amounts implied
# when a purchase costs more than the cash on hand. Without this the balance
# can't be reconciled -- money would appear from nowhere every time a buy was
# larger than the balance, and there'd be no way to tell afterwards how much
# of the portfolio was funded rather than earned.
DEPOSITS_KEY = "DEPOSITS"
RESERVED_HOLDINGS_KEYS = {CASH_KEY, REALIZED_PNL_KEY, DEPOSITS_KEY}

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
# A CORRECTION, not a trade. "added 100 shares at 12" blends into the average
# cost and moves cash; this overwrites the position outright and leaves cash
# alone, exactly like "set cash to X" does for the balance.
#
# Needed because the two are genuinely different operations and only one was
# available. Reconciling against a broker statement -- or repairing a position
# after the bot dropped a write -- is a correction, and doing it with "added"
# blends the correct number into the wrong one.
SET_POSITION_RE = re.compile(
    rf"^\s*set\s+{_TICKER}\s+(?:to\s+|=\s*)?({_NUM})\s+shares?"
    rf"(?:\s+(?:at|@)\s+\$?({_NUM}))?\s*\.?\s*$",
    re.IGNORECASE)

ADD_WATCHLIST_RE = re.compile(r"^\s*add\s+(.+?)\s+to\s+my\s+watchlist\.?\s*$", re.IGNORECASE)
REMOVE_WATCHLIST_RE = re.compile(r"^\s*remove\s+(.+?)\s+from\s+my\s+watchlist\.?\s*$", re.IGNORECASE)
WATCHLIST_RE = re.compile(r"^\s*watchlist\.?\s*$", re.IGNORECASE)
SUMMARY_RE = re.compile(r"^\s*summary\s*\.?\s*$", re.IGNORECASE)
# Cash management. "set cash to X" is the manual correction used to bootstrap
# the balance; "deposited X" is the ongoing one, and unlike a correction it
# also credits DEPOSITS so the funding total stays honest.
SET_CASH_RE = re.compile(rf"^\s*set\s+cash\s+(?:to\s+)?\$?({_NUM})\s*\.?\s*$", re.IGNORECASE)
SET_DEPOSITS_RE = re.compile(
    rf"^\s*set\s+(?:deposits|money\s+in)\s+(?:to\s+)?\$?({_NUM})\s*\.?\s*$", re.IGNORECASE)
DEPOSIT_RE = re.compile(
    rf"^\s*(?:deposit(?:ed)?|added\s+cash(?:\s+of)?)\s+\$?({_NUM})\s*\.?\s*$", re.IGNORECASE)
WITHDRAW_RE = re.compile(
    rf"^\s*(?:withdrew|withdraw|withdrawn)\s+\$?({_NUM})\s*\.?\s*$", re.IGNORECASE)
EARNINGS_TODAY_RE = re.compile(r"^\s*earnings\s+today\.?\s*$", re.IGNORECASE)
EARNINGS_FOR_RE = re.compile(r"^\s*earnings\s+for\s+(.+?)\.?\s*$", re.IGNORECASE)
STATUS_RE = re.compile(r"^\s*(?:status|health|are you (?:ok|alive|working))\??\s*$",
                       re.IGNORECASE)
HELP_RE = re.compile(r"^\s*(?:help|commands|\?|/help|/start|what can you do)\.?\s*$",
                     re.IGNORECASE)

HELP_TEXT = (
    "*What I understand*\n\n"
    "*Positions*\n"
    "added 10 shares of NVDA at 500\n"
    "sold 10 shares of NVDA at 600\n"
    "set NVDA to 710 shares at 12.70   _(correction, cash untouched)_\n"
    "summary\n\n"
    "*Cash*\n"
    "deposited 2000  /  withdrew 1000\n"
    "set cash to 10500  /  set deposits to 40000\n\n"
    "*Lists*\n"
    "add GENI to my list  /  remove GENI from my list\n"
    "add GENI, XPEV to my watchlist  /  remove GENI from my watchlist\n"
    "watchlist\n\n"
    "*Earnings*\n"
    "earnings today\n"
    "earnings for XPEV\n\n"
    "*Diagnostics*\n"
    "status\n\n"
    "_Tickers are case-insensitive and a leading $ is fine._"
)


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


class TelegramConflict(Exception):
    """Another getUpdates connection is open for this bot.

    Telegram permits exactly one. This happens for a few seconds when a new
    listener replaces an outgoing one (concurrency cancel-in-progress), and
    resolves itself once the old connection drops.
    """


def get_updates(offset: int | None, timeout: int = 0) -> list[dict]:
    """Fetch updates. `timeout` > 0 makes this a LONG POLL.

    With timeout=0 Telegram answers immediately, which is why the old
    per-run design could only ever be as fast as its cron. With timeout=25
    the connection is held open and Telegram pushes the moment a message
    arrives -- so a reply goes out about a second after you send it, and an
    idle hour costs ~144 requests instead of one every 15 seconds.

    The HTTP read timeout must exceed the long-poll timeout, or the client
    hangs up on a perfectly healthy idle connection every single time.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    resp = requests.get(url, params=params, timeout=timeout + 15)
    if resp.status_code == 409:
        raise TelegramConflict(resp.text[:200])
    resp.raise_for_status()
    return resp.json().get("result", [])


def acknowledge(offset: int) -> None:
    """Tell Telegram everything below `offset` is handled, and mean it.

    Telegram deletes an update server-side once you request the next one. That
    makes the acknowledgement authoritative in a way a git commit never was:
    the old design recorded progress by pushing state.json, so a run that
    replied at 40s but was cancelled before committing at 60s left the message
    looking unhandled -- and the next run answered it a second time. That is
    the duplicate-reply bug, and it is why the poll interval had a two-minute
    floor.

    Calling this immediately after each reply shrinks that window from about
    twenty seconds to the length of one HTTP round trip.
    """
    try:
        get_updates(offset, timeout=0)
    except Exception as e:
        # Non-fatal: the offset is still held in memory and written to
        # state.json, so at worst we fall back to the old behaviour.
        print(f"ack failed (non-fatal): {type(e).__name__}: {e}")


def send_summary(holdings: dict) -> None:
    cash = holdings.get(CASH_KEY, 0.0)
    realized_pnl = holdings.get(REALIZED_PNL_KEY, 0.0)
    deposits = holdings.get(DEPOSITS_KEY, 0.0)
    tickers_held = sorted(
        t
        for t, pos in holdings.items()
        if t not in RESERVED_HOLDINGS_KEYS and isinstance(pos, dict) and pos.get("shares", 0) > 0
    )
    if not tickers_held and not cash and not realized_pnl and not deposits:
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

    # "Cash from sales" was accurate when nothing ever decreased it. Purchases
    # now deduct, so it's a real balance and the old label would mislead.
    if cash:
        lines.append(f"*Cash on hand*: {format_usd(cash)}")
    if deposits:
        lines.append(f"*Net deposited*: {format_usd(deposits)}")
    if realized_pnl:
        arrow = "\U0001F7E2" if realized_pnl >= 0 else "\U0001F534"
        lines.append(f"*Realized P&L (sales)*: {format_usd_signed(realized_pnl)} {arrow}")
    if have_market_total:
        total_value = total_market + cash
        lines.append(f"*Portfolio value (stocks + cash)*: {format_usd(total_value)}")
        # The only figure that answers "am I actually up?". Book value ignores
        # money added along the way; against net deposits, a gain is a gain.
        if deposits:
            net = total_value - deposits
            arrow = "\U0001F7E2" if net >= 0 else "\U0001F534"
            lines.append(
                f"*Vs. money in*: {format_usd_signed(net)} "
                f"({format_pct_signed(net / deposits * 100)}) {arrow}"
            )

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

    # Finnhub's calendar is advisory here, never a gate. A watch now runs for
    # 24 hours, so most commands are deliberately armed the day BEFORE the
    # report -- a "not reporting today" check would reject exactly the usage
    # this feature exists for. It also routinely omits recent IPOs and
    # foreign issuers, so letting it veto would block real reporters.
    # Detection is the IR feed's job; this line just adds context.
    calendar_note = ""
    calendar_rows = fetch_earnings_calendar_finnhub(today)
    if calendar_rows:
        reporting_today = {row.get("symbol") for row in calendar_rows if row.get("symbol")}
        not_listed = [t for t in valid if t not in reporting_today]
        if not_listed:
            verb = "isn't" if len(not_listed) == 1 else "aren't"
            calendar_note = (
                f"\n\nHeads-up: per Finnhub's calendar {', '.join(not_listed)} {verb} "
                f"listed as reporting today ({today}). Watching anyway."
            )

    # Every ticker is watchable now. Detection falls back to Google News where
    # there's no IR feed, so there's no longer a supported/unsupported split --
    # only a difference in how detailed the resulting summary will be, which
    # the confirmation message below is honest about.
    # Same helper the automatic holdings jobs use, so a watch armed by hand
    # and one armed by the calendar are identical records handled by one code
    # path. An already-active watch is left alone rather than reset, which
    # would wipe the record of what it has already sent you.
    armed_any = False
    caught_up = False
    for ticker in valid:
        key = f"ew_watch::{ticker.upper()}"
        if arm_earnings_watch(state, ticker, ON_DEMAND_WATCH_HOURS):
            armed_any = True

        # Report anything ALREADY filed today before baselining it away.
        #
        # Without this, texting "earnings for XPEV" after the company has
        # filed does nothing useful: the watch is armed, the first poll marks
        # the existing filing as the baseline, and you wait 24 hours for a
        # "nothing appeared" notice about results published hours ago. Which
        # is exactly the position this command is reached from -- you text it
        # BECAUSE you think something was missed.
        #
        # This runs even when the watch ALREADY EXISTS, which is the case that
        # actually bit. An existing watch makes arm_earnings_watch return
        # False, so gating the catch-up on "a new watch was created" turned
        # the retry into a silent no-op: the one command available to fix a
        # miss did nothing at all, precisely because the first attempt had
        # already armed something. `hit` keeps it from re-sending.
        if state.get(key, {}).get("hit"):
            print(f"[{ticker}] results already sent for this watch.")
            continue
        try:
            import earnings_watch
            if earnings_watch._set_baseline_now(state, ticker):
                state[key]["hit"] = True
                caught_up = True
        except Exception as e:
            print(f"[{ticker}] catch-up check failed: {type(e).__name__}: {e}")

    # The watcher exits when nothing is armed, so starting one is what keeps
    # an on-demand command fast -- without it you'd wait for the next hourly
    # run, up to ~55 minutes.
    #
    # Registered rather than fired, because the watcher reads state.json from
    # a fresh checkout of main. Dispatching before the push is a race the
    # watcher can lose: it starts, sees no armed watch, and exits. It fires in
    # _Session.flush(), immediately after the push lands.
    if armed_any:
        request_watcher_start()

    if caught_up:
        note = " Results already filed today are above."
    elif not armed_any:
        note = " (already watching; nothing filed yet today)"
    else:
        note = ""
    send_telegram_message(
        f"\U0001F514 Watching {', '.join(valid)} for the next "
        f"{ON_DEMAND_WATCH_HOURS}h. I check their SEC filings every 15 seconds "
        f"and will send results within seconds of them being filed." + note
        + calendar_note
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


def build_status(state: dict) -> str:
    """One message answering "is it working?".

    Reads state fresh from origin/main first. The listener holds its own copy
    for hours, and the components reported on here -- the monitor, the arm
    job, the watcher -- write their timestamps from other runners entirely.
    Asking the in-memory copy would describe the listener's view of several
    hours ago, which is exactly the mistake that made pruned keys reappear.
    """
    import health
    import repo_commit
    import workflow_trigger

    if repo_commit.refresh_from_origin("state.json"):
        state = load_state()

    lines = ["*Status*", ""]

    tick, warn = "\u2705", "\u26a0\ufe0f"
    ok_all = True
    # The watcher's heartbeat is only committed when its run ends, so for
    # that component the Actions API is the truthful source and the
    # timestamp is the fallback. See health.EXIT_PERSISTED.
    for name, detail, ok in health.component_lines(
            state, latest_run=workflow_trigger._latest_run):
        ok_all = ok_all and ok
        lines.append(f"{tick if ok else warn} {name}: {detail}")

    lines.append("")
    lines.append("*Last alert sent*")
    for kind, age in health.alert_lines(state):
        lines.append(f"  {kind}: {age}")

    watches = sorted(k.split("::", 1)[1] for k in state if k.startswith("ew_watch::"))
    lines.append("")
    lines.append(f"*Earnings watches armed*: {', '.join(watches) if watches else 'none'}")

    providers = [n for n, key in (("Groq", "GROQ_API_KEY"), ("Gemini", "GEMINI_API_KEY"))
                 if os.environ.get(key)]
    lines.append(f"*Model*: {', '.join(providers) if providers else 'NONE -- keyword fallback'}")

    if not ok_all:
        lines.append("")
        lines.append("_A component is stale. If it stays that way, check "
                     "Actions for that workflow._")
    return "\n".join(lines)


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

    # Cash commands are checked BEFORE the share commands. "added cash 5000"
    # would otherwise be caught by nothing at all, and a future loosening of
    # the share regex could swallow it silently.
    set_cash_match = SET_CASH_RE.match(text)
    if set_cash_match:
        amount = parse_num(set_cash_match.group(1))
        previous = holdings.get(CASH_KEY, 0.0)
        holdings[CASH_KEY] = amount

        seeded_note = ""
        if DEPOSITS_KEY not in holdings:
            # First time only: seed the funding total with this cash PLUS the
            # cost of everything already held. Those shares were paid for with
            # money that predates the bot, and leaving it out would make every
            # return figure look enormous -- comparing today's portfolio
            # against nothing but later top-ups.
            book = sum(
                pos.get("shares", 0.0) * pos.get("avg_cost", 0.0)
                for t, pos in holdings.items()
                if t not in RESERVED_HOLDINGS_KEYS and isinstance(pos, dict)
            )
            holdings[DEPOSITS_KEY] = amount + book
            seeded_note = (
                f"\n\nStarting money-in set to {format_usd(amount + book)} "
                f"— this cash plus the {format_usd(book)} cost of what you "
                f"already hold. Correct it with \"set deposits to X\" if that's off."
            )

        send_telegram_message(
            f"✅ Cash on hand set to {format_usd(amount)} "
            f"(was {format_usd(previous)}).{seeded_note}"
        )
        return False, True, False

    set_deposits_match = SET_DEPOSITS_RE.match(text)
    if set_deposits_match:
        amount = parse_num(set_deposits_match.group(1))
        previous = holdings.get(DEPOSITS_KEY, 0.0)
        holdings[DEPOSITS_KEY] = amount
        send_telegram_message(
            f"✅ Total money in set to {format_usd(amount)} "
            f"(was {format_usd(previous)}). Cash on hand is unchanged at "
            f"{format_usd(holdings.get(CASH_KEY, 0.0))}."
        )
        return False, True, False

    deposit_match = DEPOSIT_RE.match(text)
    if deposit_match:
        amount = parse_num(deposit_match.group(1))
        holdings[CASH_KEY] = holdings.get(CASH_KEY, 0.0) + amount
        holdings[DEPOSITS_KEY] = holdings.get(DEPOSITS_KEY, 0.0) + amount
        send_telegram_message(
            f"✅ Deposited {format_usd(amount)}.\n"
            f"Cash on hand: {format_usd(holdings[CASH_KEY])}  |  "
            f"Total deposited: {format_usd(holdings[DEPOSITS_KEY])}"
        )
        return False, True, False

    withdraw_match = WITHDRAW_RE.match(text)
    if withdraw_match:
        amount = parse_num(withdraw_match.group(1))
        holdings[CASH_KEY] = holdings.get(CASH_KEY, 0.0) - amount
        # Withdrawals reduce net deposits, so "total deposited" stays a
        # meaningful measure of what you've actually put in.
        holdings[DEPOSITS_KEY] = holdings.get(DEPOSITS_KEY, 0.0) - amount
        send_telegram_message(
            f"✅ Withdrew {format_usd(amount)}.\n"
            f"Cash on hand: {format_usd(holdings[CASH_KEY])}  |  "
            f"Net deposited: {format_usd(holdings[DEPOSITS_KEY])}"
        )
        return False, True, False

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

        # Pay for the shares out of cash. If the purchase costs more than is
        # on hand, assume the shortfall was deposited rather than letting the
        # balance go negative -- money had to come from somewhere for the
        # trade to have happened. The implied amount is added to DEPOSITS so
        # the balance still reconciles and you can see how much was funded.
        cost = qty * price
        cash = holdings.get(CASH_KEY, 0.0)
        shortfall = max(0.0, cost - cash)
        if shortfall > 0:
            holdings[DEPOSITS_KEY] = holdings.get(DEPOSITS_KEY, 0.0) + shortfall
            cash += shortfall
        holdings[CASH_KEY] = cash - cost

        funding_note = ""
        if shortfall > 0:
            funding_note = (
                f"\nThat cost {format_usd(cost)}, more than your cash on hand, "
                f"so I assumed a deposit of {format_usd(shortfall)}."
            )
        watch_note = " Also added it to your holdings list for price/news/earnings alerts." if tickers_changed else ""
        send_telegram_message(
            f"✅ Added {qty:,.0f} shares of *{ticker}* at {format_usd(price)}.\n"
            f"New position: {new_shares:,.0f} sh @ avg {format_usd(new_avg)} "
            f"(book {format_usd(new_shares * new_avg)}).{funding_note}\n"
            f"Cash on hand: {format_usd(holdings[CASH_KEY])}.{watch_note}"
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
        send_telegram_message(f"\U0001F5D1 Removed *{ticker}* from your holdings list.")
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

    set_position_match = SET_POSITION_RE.match(text)
    if set_position_match:
        ticker = set_position_match.group(1).upper()
        qty = parse_num(set_position_match.group(2))
        price_raw = set_position_match.group(3)

        if ticker in RESERVED_HOLDINGS_KEYS:
            send_telegram_message(
                f"*{ticker}* is a reserved name and can't be used as a ticker symbol.")
            return False, False, False

        old = holdings.get(ticker, {})
        old_shares = old.get("shares", 0.0)
        old_avg = old.get("avg_cost", 0.0)

        if price_raw is None:
            if not old_avg:
                send_telegram_message(
                    f"I don't have an average cost for *{ticker}*, so I need one: "
                    f'try "set {ticker} to {qty:,.0f} shares at $12.34".')
                return False, False, False
            price = old_avg
        else:
            price = parse_num(price_raw)

        tickers_changed = False
        if qty > 0 and ticker not in tickers:
            if not validate_ticker(ticker):
                send_telegram_message(
                    f"Couldn't find market data for *{ticker}* -- "
                    f"double-check the symbol and try again.")
                return False, False, False
            tickers.append(ticker)
            tickers_changed = True

        if qty == 0:
            holdings.pop(ticker, None)
            send_telegram_message(
                f"\U0001F4DD Cleared *{ticker}* (was {old_shares:,.0f} sh @ "
                f"{format_usd(old_avg)}). Cash and deposits unchanged.")
            return tickers_changed, True, False

        holdings[ticker] = {"shares": qty, "avg_cost": price}
        was = (f"was {old_shares:,.0f} sh @ {format_usd(old_avg)}"
               if old_shares else "no previous position")
        send_telegram_message(
            f"\U0001F4DD Set *{ticker}* to {qty:,.0f} sh @ {format_usd(price)} "
            f"(book {format_usd(qty * price)}).\n"
            f"_{was}._\n"
            f"Cash and deposits unchanged -- this is a correction, not a trade."
        )
        return tickers_changed, True, False

    if STATUS_RE.match(text):
        send_telegram_message(build_status(state))
        return False, False, False

    if HELP_RE.match(text):
        send_telegram_message(HELP_TEXT)
        return False, False, False

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

    # Nothing matched. Say so.
    #
    # This used to `return False, False, False` and send nothing at all, which
    # is indistinguishable from the bot being down -- and it HAS been down,
    # repeatedly, so silence is the one response that cannot be interpreted.
    # A near-miss on a command's wording ("sold 10 NVDA at 600", missing
    # "shares of") produced exactly the same nothing as a dead listener.
    if text and text.strip():
        print(f"Unrecognised command: {text[:80]!r}")
        send_telegram_message(
            "I didn't understand that one.\n\n" + HELP_TEXT
        )
    return False, False, False


# --- Deferred watcher dispatch ------------------------------------------
#
# Arming an earnings watch has to start the watcher, because the watcher
# exits when nothing is armed. But the watcher reads state.json from a FRESH
# CHECKOUT of main, so dispatching before state.json is pushed is a race: the
# watcher can start, see no armed watch, and exit -- leaving the report
# uncovered until the next hourly run, or until tomorrow outside watch hours.
#
# So handlers register the intent here, and it is fired only after the push.
_watcher_wanted = False


def request_watcher_start() -> None:
    global _watcher_wanted
    _watcher_wanted = True


def _start_watcher_if_requested() -> None:
    global _watcher_wanted
    if not _watcher_wanted:
        return
    _watcher_wanted = False
    try:
        from workflow_trigger import start_earnings_watcher
        start_earnings_watcher()
    except Exception as e:
        print(f"Could not start earnings watcher: {type(e).__name__}: {e}")


class _Session:
    """Everything a run mutates, so the loop can hold it across iterations."""

    def __init__(self):
        self.state = load_state()
        # What state looked like when we read it. Everything else is measured
        # against this, so the listener writes DELTAS rather than a snapshot.
        self._baseline = copy.deepcopy(self.state)
        self.tickers = load_tickers()
        self.holdings = load_holdings()
        self.watchlist = load_watchlist()
        self.offset = self.state.get("tg_update_offset")
        self.tickers_changed = False
        self.holdings_changed = False
        self.watchlist_changed = False
        self.state_changed = False

    @property
    def dirty(self) -> bool:
        return (self.tickers_changed or self.holdings_changed
                or self.watchlist_changed or self.state_changed)

    def flush(self) -> None:
        """Write changed files, push them, THEN dispatch any watcher."""
        if self.tickers_changed:
            save_tickers(self.tickers)
            print(f"tickers.json updated: {self.tickers}")
        holdings_dirty = self.holdings_changed
        if holdings_dirty:
            save_holdings(self.holdings)
            print("holdings.json updated (values redacted -- this repo is public)")
        if self.watchlist_changed:
            save_watchlist(self.watchlist)
            print(f"watchlist.json updated: {self.watchlist}")

        self.state["tg_update_offset"] = self.offset

        # Work out what WE changed, then re-apply it on top of whatever the
        # repository says now. Writing self.state wholesale would revert
        # every change the monitor has made since this listener started --
        # it runs for hours, the monitor writes the same file every minute.
        changed = {k: v for k, v in self.state.items()
                   if k not in self._baseline or self._baseline[k] != v}
        deleted = [k for k in self._baseline if k not in self.state]

        import repo_commit
        if repo_commit.refresh_from_origin("state.json"):
            fresh = load_state()
            for key in deleted:
                fresh.pop(key, None)
            fresh.update(changed)
            self.state = fresh

        save_state(self.state)
        self._baseline = copy.deepcopy(self.state)

        repo_commit.commit_and_push(
            ["tickers.json", "watchlist.json", "state.json"],
            "Update tickers/watchlist/state via Telegram command [skip ci]")

        # holdings.json lives in the SEPARATE private repo checked out at
        # data/, and must be committed here rather than left to the
        # workflow's final step.
        #
        # That step only runs when the job finishes normally. The listener
        # loops for hours and is cancelled by design -- every hour by the
        # cron, and by the watchdog -- so it had stopped running at all. The
        # trade was applied in memory, written to the runner's disk, and
        # correctly acknowledged over Telegram; then the runner was destroyed
        # and the private repo still held the old numbers. The next listener
        # loaded those, so `summary` showed stale share counts and any later
        # buy or sell computed from the wrong base.
        if holdings_dirty:
            repo_commit.commit_and_push(
                ["holdings.json"],
                "Update holdings via Telegram command [skip ci]",
                cwd=os.path.dirname(HOLDINGS_FILE),
                merged_file="holdings.json")

        self.tickers_changed = self.holdings_changed = False
        self.watchlist_changed = self.state_changed = False

        _start_watcher_if_requested()


def handle_updates(updates: list[dict], session: _Session) -> None:
    """Process a batch, acknowledging each message as soon as it is answered."""
    for update in updates:
        session.offset = update["update_id"] + 1
        session.state_changed = True

        message = update.get("message") or update.get("edited_message")
        if not message:
            acknowledge(session.offset)
            continue

        chat_id = str(message.get("chat", {}).get("id", ""))
        if str(TELEGRAM_CHAT_ID) != chat_id:
            print(f"Ignoring message from unexpected chat_id={chat_id}")
            acknowledge(session.offset)
            continue

        text = message.get("text", "")
        try:
            t_changed, h_changed, w_changed = process_message(
                text, session.tickers, session.holdings,
                session.watchlist, session.state)
        except Exception as e:
            # One bad command must not kill an hour-long listener, and must
            # not leave the message unacknowledged to be retried forever.
            print(f"Command failed: {type(e).__name__}: {e}")
            t_changed = h_changed = w_changed = False

        session.tickers_changed |= t_changed
        session.holdings_changed |= h_changed
        session.watchlist_changed |= w_changed

        # Acknowledged only AFTER the reply has been sent.
        acknowledge(session.offset)


def _code_has_changed() -> bool:
    """Has any Python file on origin/main moved since this run checked out?

    A long-running job runs the code it started with. That was harmless at 62
    minutes and is not at 330: a fix pushed at 15:45 would not take effect
    until the loop ended at 21:10, so the bot keeps giving the old answer long
    after the bug is fixed. That happened the first time this was used -- the
    `set` command was live on main and the running listener had never heard
    of it, so it replied "I didn't understand that one".

    Compares only *.py, deliberately. state.json is committed every few
    minutes by the monitor and by this listener itself; watching every commit
    would restart the loop continuously.
    """
    try:
        subprocess.run(["git", "fetch", "--quiet", "origin", "main"],
                       check=True, capture_output=True, timeout=30)
        diff = subprocess.run(
            ["git", "diff", "--quiet", "HEAD", "origin/main", "--", "*.py"],
            capture_output=True, timeout=30)
        return diff.returncode != 0
    except Exception as e:
        # Never fatal: a listener that cannot check for updates is still a
        # working listener.
        print(f"Update check failed (non-fatal): {type(e).__name__}: {e}")
        return False


def listen() -> None:
    """Hold an open connection to Telegram and answer as messages arrive.

    Replaces a cron firing every couple of minutes. The per-run design spent
    40-63 seconds provisioning a runner, checking out two repos and installing
    packages in order to do about four seconds of work -- so polling faster
    just repeated the overhead, and firing faster than a run completes queued
    runs into a lane that cancelled them.

    Paying that startup once an hour instead takes the reply from up to two
    minutes down to a second or two. This mirrors earnings_watch.py, which
    made the same move for the same reason.
    """
    deadline = time.monotonic() + LOOP_MINUTES * 60
    cycles = 0
    session = _Session()
    heartbeat.ping(heartbeat.LISTENER)   # connected
    health.record(session.state, "listener")
    session.state_changed = True
    session.flush()
    print(f"Listening for {LOOP_MINUTES} minutes "
          f"(long poll {POLL_TIMEOUT_SECONDS}s), offset={session.offset}.")

    idle_backoff = 0

    while time.monotonic() < deadline:
        try:
            updates = get_updates(session.offset, timeout=POLL_TIMEOUT_SECONDS)
            idle_backoff = 0
        except TelegramConflict:
            # The outgoing listener has not dropped its connection yet.
            # Expected at handover; back off rather than fight it.
            print("Another listener is still connected; waiting.")
            time.sleep(5)
            continue
        except Exception as e:
            idle_backoff = min(idle_backoff * 2 + 2, 60)
            print(f"getUpdates failed: {type(e).__name__}: {e} "
                  f"-- retrying in {idle_backoff}s")
            time.sleep(idle_backoff)
            continue

        cycles += 1

        # Proof of life while idle. The listener's failure mode is ABSENCE --
        # it was missing for half of 2026-08-24 and nothing noticed -- and an
        # idle listener sends nothing else, so silence is otherwise
        # indistinguishable from death.
        if cycles % UPDATE_CHECK_EVERY_N_CYCLES == 0:
            heartbeat.ping(heartbeat.LISTENER)
            health.record(session.state, "listener")
            session.state_changed = True
            session.flush()

        # Roughly every ten minutes of idling.
        if cycles % UPDATE_CHECK_EVERY_N_CYCLES == 0 and _code_has_changed():
            print("New code on origin/main -- exiting so a fresh run picks it up.")
            if session.dirty:
                session.flush()
            try:
                from workflow_trigger import restart_listener
                restart_listener()
            except Exception as e:
                print(f"Restart dispatch failed: {type(e).__name__}: {e}")
            return

        if not updates:
            continue

        print(f"{len(updates)} update(s).")
        handle_updates(updates, session)
        if session.dirty:
            session.flush()

    # The offset advances even on a quiet hour only if something arrived;
    # flush anyway so a clean exit records where we got to.
    if session.dirty:
        session.flush()
    print("Listener finished its window; the workflow will start a fresh one.")


def main() -> None:
    """One-shot mode, kept for manual dispatch and as a fallback."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; skipping.")
        return

    session = _Session()
    updates = get_updates(session.offset)
    if not updates:
        print("No new Telegram messages.")
        return

    handle_updates(updates, session)
    session.flush()


if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; skipping.")
    elif len(sys.argv) > 1 and sys.argv[1] == "listen":
        listen()
    else:
        main()
