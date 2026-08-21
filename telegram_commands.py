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
"earnings for <TICKER, TICKER, ...>" arms a 24-hour watch on each symbol and
sends you EVERYTHING that company publishes on its reporting day, with a link
to each item. Any ticker works, held or not.

The rule is the date, not keywords. On the day a company reports, the release
is essentially the only thing it posts, so "dated today" is far more reliable
than judging whether a headline sounds like earnings -- the classifier this
replaced nearly missed the real Cerebras release, whose headline contained no
results word at all.

Because everything from that day counts, the watch does NOT close on the
first article: a routine morning announcement would otherwise end it and the
actual results, hours later, would never arrive. It runs the full 24 hours
and remembers what it has sent, so nothing repeats on the next poll.

Where it looks, in order (ir_page.articles_for): the company's RSS feed if it
has one, otherwise its investor-relations page's HTML, otherwise news
headlines. The IR page is discovered from the ticker -- website via yfinance,
then the "investor relations" link on it -- and cached in state, since that
chain is far too slow to repeat once a minute. Only the news fallback still
uses keyword matching, because dozens of articles mention a ticker every day
and a date rule would be meaningless there.

Polling happens only inside ON_DEMAND_POLL_WINDOWS_ET -- late afternoon for
after-close reporters, early morning for before-open ones. The 24-hour span
is the point: detection used to be same-day only, so a company reporting
pre-market at ~6am meant texting the bot overnight. Now the command can be
sent the previous afternoon and either release time is covered.

Finnhub is consulted for the earnings *calendar* only, and its answer is
advisory: if it doesn't list a ticker as reporting today you get a note
saying so, but the watch is armed regardless. A feed can only tell you
something has happened, never that it is scheduled -- and Finnhub's calendar
has been wrong often enough that letting it veto a watch would be worse than
the occasional wasted one.

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
from datetime import datetime, timedelta

import requests
import yfinance as yf

import ir_feeds
import ir_page
from config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    ON_DEMAND_WATCH_HOURS,
    ON_DEMAND_POLL_WINDOWS_ET,
)
from telegram_utils import send_telegram_message, escape_markdown
from state_utils import load_state, save_state
from earnings_utils import now_et, date_str_et, fetch_earnings_calendar_finnhub
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
    now = now_et()
    expires = now + timedelta(hours=ON_DEMAND_WATCH_HOURS)
    for ticker in valid:
        state[f"ew_watch::{ticker}"] = {
            "armed": now.isoformat(),
            "expires": expires.isoformat(),
        }

    # No per-ticker prediction of which source will be used: that's resolved
    # at poll time by ir_page.articles_for, and guessing here would sometimes
    # be wrong in a message you'd reasonably trust.
    windows = ", ".join(f"{a}-{b}" for a, b in ON_DEMAND_POLL_WINDOWS_ET)
    send_telegram_message(
        f"\U0001F514 Watching {', '.join(valid)} for the next "
        f"{ON_DEMAND_WATCH_HOURS}h. I'll check every minute during {windows} ET "
        f"and send you anything they publish that day, with a link."
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


def _in_poll_window(now) -> bool:
    """True if `now` (ET) falls inside one of ON_DEMAND_POLL_WINDOWS_ET.

    Releases land either just after the 4pm close or before the 9:30 open, so
    polling the other ~19 hours a day would burn runs for nothing. Note the
    windows are compared on wall-clock time only -- a window never spans
    midnight, so no wraparound handling is needed. Keep it that way; if a
    window like 22:00-02:00 is ever wanted, this needs to change.
    """
    minutes = now.hour * 60 + now.minute
    for start, end in ON_DEMAND_POLL_WINDOWS_ET:
        sh, sm = (int(p) for p in start.split(":"))
        eh, em = (int(p) for p in end.split(":"))
        if sh * 60 + sm <= minutes <= eh * 60 + em:
            return True
    return False


def check_on_demand_earnings(state: dict) -> bool:
    """Called on every run. Sends anything a watched company publishes on its
    reporting day. Returns whether state was mutated.

    The rule is the date, not keywords. On the day a company reports, the
    release is essentially the only thing it posts, so "dated today" beats
    "this headline sounds like earnings" -- the classifier this replaces
    nearly missed the real Cerebras release, whose headline contained no
    results word at all.

    Sources, in order of preference (see ir_page.articles_for): the company's
    RSS feed if it has one, otherwise its IR page's HTML, otherwise news
    headlines. Not Finnhub, whose epsActual lagged the Cerebras release by a
    whole evening, and not SEC EDGAR, which 403s GitHub Actions runners.

    A watch is armed by "earnings for TICKER" and lives for
    ON_DEMAND_WATCH_HOURS -- deliberately longer than a day, so a company
    reporting pre-market at ~6am doesn't require sending the command
    overnight. The two poll windows straddle both the close and the open.

    IMPORTANT: a watch does NOT close on the first article. Everything the
    company says that day is wanted, so closing on a routine morning
    announcement would mean the actual results, hours later, never arrive.
    It runs until it expires, and remembers what it has already sent so
    nothing repeats on the next poll a minute later.

    Expiry is checked against a stored timestamp rather than elapsed time,
    because each run is a separate short-lived process.
    """
    now = now_et()
    watches = {k: v for k, v in state.items() if k.startswith("ew_watch::")}
    if not watches:
        return False

    changed = False
    in_window = _in_poll_window(now)

    for key, watch in watches.items():
        ticker = key.split("::", 1)[1]

        # Expiry is checked even outside a poll window, so a dead watch is
        # cleaned up promptly instead of lingering until the next window.
        try:
            expires = datetime.fromisoformat(watch["expires"])
            armed = datetime.fromisoformat(watch["armed"])
        except (KeyError, TypeError, ValueError):
            print(f"[{ticker}] malformed watch record, dropping: {watch!r}")
            del state[key]
            changed = True
            continue

        sent = set(watch.get("sent", []))

        if now >= expires:
            # Only worth flagging if the watch produced nothing at all. If it
            # already sent you the day's announcements, a "nothing found"
            # message would be plainly wrong.
            if not sent:
                send_telegram_message(
                    f"\u26A0\uFE0F *{ticker}*: nothing was published on their "
                    f"investor-relations page in the last {ON_DEMAND_WATCH_HOURS}h. "
                    f"They may have pushed the date, or their site may not be "
                    f"readable automatically -- worth a manual look."
                )
            del state[key]
            changed = True
            continue

        if not in_window:
            continue

        today = now.date()
        articles, source = ir_page.articles_for(ticker, state)

        if source is None:
            # No feed and no readable IR page -- Genius Sports and AppLovin
            # serve nothing to an automated reader even in a real browser.
            # News headlines are the only route left, and the date rule can't
            # be used there: dozens of articles mention a ticker every day, so
            # this path keeps the keyword classifier.
            release = ir_feeds.find_release_google(ticker, armed, state)
            if release and release["link"] not in sent:
                send_telegram_message(ir_feeds.build_message(release))
                sent.add(release["link"])
                watch["sent"] = sorted(sent)
                state[key] = watch
                changed = True
            continue

        fresh = ir_page.todays_articles(articles, sent, today)
        if not fresh:
            continue

        print(f"[{ticker}] {len(fresh)} new article(s) dated {today} via {source}.")
        for article in fresh:
            send_telegram_message(_build_ir_message(ticker, article, source))
            sent.add(article["url"])

        # Persist immediately rather than at the end of the loop: this is what
        # stops the same article being re-sent on the next poll a minute later.
        watch["sent"] = sorted(sent)
        state[key] = watch
        changed = True

    return changed


def _build_ir_message(ticker: str, article: dict, source: str) -> str:
    """One article, as posted by the company on its reporting day.

    No beat/miss analysis: the figures live behind the link, and the runner
    can't reliably fetch article bodies. The value here is speed and the URL.
    """
    label = ("investor-relations page" if source == "page"
             else "investor-relations feed")
    return (
        f"\U0001F4CA *{ticker} posted today*\n\n"
        f"{escape_markdown(article['title'])}\n\n"
        f"{article['url']}\n\n"
        f"_(from their {label})_"
    )


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
        print("holdings.json updated (values redacted -- this repo is public)")
    if watchlist_changed:
        save_watchlist(watchlist)
        print(f"watchlist.json updated: {watchlist}")

    save_state(state)


if __name__ == "__main__":
    main()
