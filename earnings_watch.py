"""Watches SEC filings for the earnings of stocks you own or asked about.

Run as:  python earnings_watch.py arm     (cheap; arms watches, exits)
         python earnings_watch.py poll    (long-running detection loop)

Why one long job instead of many short ones
-------------------------------------------
The previous design started a fresh GitHub Actions run per poll. Measured,
each run spent 45-100 seconds provisioning a machine, checking out the repo,
installing Python and installing yfinance -- in order to do about 2 seconds
of actual work. Polling more often simply repeated that tax.

This runs ONE job that loops internally, so startup is paid once an hour
instead of once a minute. Detection latency drops from 90-150 seconds to
roughly 15.

This is not a return to the sleep-loops removed earlier in this project.
Those held the SHARED concurrency lane and stalled the one-minute pollers,
which caused 163 cancelled runs and the duplicate-reply bug. This job has its
own lane and its own workflow, and cancels its own predecessor cleanly.

Polling discipline
------------------
Only tickers with an armed watch are polled, and only every POLL_SECONDS.
That is a deliberate cap on request volume, not on speed: polling every
ticker continuously would put roughly 37,000 requests a day onto GitHub's
shared IP ranges, and losing SEC access would cost far more than the few
seconds a tighter interval buys.

Conditional requests (ETag) mean an unchanged poll returns 304 with an empty
body, so the steady state is cheap for the SEC as well as for us.

Alerts arrive in two stages: the fact of the filing as soon as it is seen,
then the figures once the document has been fetched and parsed. There is no
reason to wait for parsing before learning that results are out.
"""

import re
import sys
import time
from datetime import datetime, timedelta, timezone

import sec_edgar
from config import ON_DEMAND_WATCH_HOURS, TICKERS
from earnings_utils import (
    arm_earnings_watch,
    classify_holdings_for_date,
    date_str_et,
    now_et,
)
from state_utils import load_state, save_state
from telegram_utils import escape_markdown, send_telegram_message

# 15 seconds, not 10. The difference in worst-case latency is about five
# seconds; the difference in request volume is roughly a third. Losing SEC
# access would remove the only universal source available, so the trade is
# not close. Revisit once the SEC's own indexing lag is measured -- if that
# turns out to be 45 seconds, nothing below a minute matters anyway.
POLL_SECONDS = 15

# LONGER than the hourly restart interval, deliberately. At 55 minutes each
# watcher ended at :00 while the next began at :05, leaving a five-minute
# hole at the top of every hour -- and TSMC and Honda both file at 06:02 ET,
# squarely inside it. A filing in the gap was still found by the next run
# (the baseline lives in state, not in the process), but up to five minutes
# late, which defeats the point of polling every fifteen seconds.
#
# Running past the hour means the incoming watcher overlaps the outgoing one.
# concurrency: cancel-in-progress is true, so the new run cancels the old on
# creation and the only remaining gap is runner provisioning, ~30 seconds.
LOOP_MINUTES = 62

_FIGURE_PATTERNS = (
    ("Revenue", r"revenues?\s+(?:of\s+)?(?:was\s+)?\$?([\d.,]+\s*(?:billion|million)?)"),
    ("Net income", r"net income\s+(?:of\s+)?\$?([\d.,]+\s*(?:billion|million)?)"),
    ("Net loss", r"net loss\s+(?:of\s+)?\$?([\d.,]+\s*(?:billion|million)?)"),
    ("EPS", r"(?:diluted\s+)?(?:earnings|loss)\s+per\s+share\s+(?:of\s+)?\$?([\d.,()]+)"),
)


def _watches(state):
    return {k: v for k, v in state.items() if k.startswith("ew_watch::")}


def arm() -> None:
    """Arm watches for holdings the calendar says report today or tomorrow.

    Finnhub supplies the calendar and nothing else. Knowing a report is
    SCHEDULED is the one thing filings can never tell you, since a filing
    only ever reports what has already happened.
    """
    state = load_state()
    changed = False

    for offset, label in ((0, "today"), (1, "tomorrow")):
        day = date_str_et(offset)
        try:
            classification = classify_holdings_for_date(day, TICKERS)
        except Exception as e:
            print(f"Calendar lookup failed for {day}: {type(e).__name__}: {e}")
            continue

        for ticker, category in classification.items():
            if category not in ("bmo", "amc", "unsupplied"):
                continue
            if arm_earnings_watch(state, ticker, ON_DEMAND_WATCH_HOURS):
                changed = True
                _set_baseline_now(state, ticker)
                print(f"[{ticker}] armed ({category}, reports {label})")
                send_telegram_message(
                    f"\U0001F514 *{ticker}* reports earnings {label}. "
                    f"I'm watching their SEC filings and will send results "
                    f"within seconds of them being filed."
                )

    if changed:
        save_state(state)
        # The watcher exits when idle, so arming has to start one.
        from workflow_trigger import start_earnings_watcher
        start_earnings_watcher()
    else:
        print("No new watches to arm.")


def _set_baseline_now(state, ticker):
    """Record what already exists, at ARM time rather than at first poll.

    This closes a silent-miss window. The baseline marks "everything before
    this is old", and it used to be set on the first poll. A company armed at
    06:00 that files at 06:02, with the first poll landing at 06:05, would
    have had its filing swallowed into the baseline and treated as already
    seen -- no alert, and a run that looked entirely normal in the logs.

    Setting it here means the gap between arming and polling cannot hide a
    filing, however the schedules drift.
    """
    cik_map = sec_edgar.load_cik_map()
    cik = sec_edgar.resolve_cik(ticker, cik_map)
    if not cik:
        return
    try:
        filings, _ = sec_edgar.recent_filings(cik)
    except sec_edgar.FetchError as e:
        # Leaving the key unset is the safe failure: the first poll will set
        # it instead, which is the old behaviour rather than a worse one.
        print(f"[{ticker}] baseline at arm time failed ({e}); poll will set it.")
        return
    state[f"ew_seen::{ticker}"] = filings[0]["accession"] if filings else ""


def _baseline(state, ticker, cik):
    """Newest accession, if arming didn't already record one.

    Normally set by _set_baseline_now() when the watch is armed. This is the
    fallback for watches armed by the Telegram command, and for the case
    where the arm-time lookup failed.
    """
    key = f"ew_seen::{ticker}"
    if key in state:
        return state[key]
    try:
        filings, _ = sec_edgar.recent_filings(cik)
    except sec_edgar.FetchError as e:
        print(f"[{ticker}] baseline fetch failed, will retry: {e}")
        return None
    newest = filings[0]["accession"] if filings else ""
    state[key] = newest
    print(f"[{ticker}] baseline {newest or '(none)'}")
    return newest


def extract_figures(text: str) -> str:
    """A few headline numbers for the follow-up message.

    Deliberately shallow. Press releases vary far too much to parse reliably,
    and a confidently wrong revenue figure is worse than none -- the link is
    always sent, so the authoritative numbers are one tap away.
    """
    out = []
    for label, pattern in _FIGURE_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            out.append(f"{label}: {m.group(1).strip()}")
    return "  |  ".join(out)


def _report(ticker, cik, filing, kind):
    """Two-stage alert: the fact first, the figures once parsed."""
    accepted = filing.get("accepted", "")
    send_telegram_message(
        f"\U0001F4CA *{ticker} earnings just filed*\n"
        f"{kind}, accepted {accepted}\n"
        f"{sec_edgar.filing_url(cik, filing['accession'], filing.get('doc'))}"
    )

    try:
        score, _, text = sec_edgar.score_filing(cik, filing["accession"])
    except sec_edgar.FetchError as e:
        print(f"[{ticker}] figures unavailable: {e}")
        return

    figures = extract_figures(text)
    if figures:
        send_telegram_message(f"*{ticker}* — {escape_markdown(figures)}")

    # The number that decides whether POLL_SECONDS is worth tightening. It
    # can only be measured against a real filing, so it is logged every time.
    try:
        filed_at = datetime.fromisoformat(accepted.replace("Z", "+00:00"))
        lag = (datetime.now(timezone.utc) - filed_at).total_seconds()
        print(f"[{ticker}] DETECTION LAG {lag:.0f}s after SEC acceptance "
              f"(score {score})")
    except (TypeError, ValueError):
        pass


def _process(state, key, watch, ticker, cik, filings, seen):
    """Handle filings newer than `seen`, oldest first. Returns True if sent."""
    fresh = []
    for f in filings:
        if f["accession"] == seen:
            break
        fresh.append(f)
    if not fresh:
        return False

    state[f"ew_seen::{ticker}"] = filings[0]["accession"]
    sent = False

    for filing in reversed(fresh):
        if sec_edgar.is_domestic_earnings(filing):
            _report(ticker, cik, filing, "8-K item 2.02")
            sent = True
        elif filing["form"] == "6-K":
            try:
                score, period, _ = sec_edgar.score_filing(cik, filing["accession"])
            except sec_edgar.FetchError as e:
                print(f"[{ticker}] 6-K scoring failed, skipping: {e}")
                continue
            if sec_edgar.is_foreign_earnings(score, period):
                _report(ticker, cik, filing, f"6-K (score {score})")
                sent = True
            else:
                print(f"[{ticker}] 6-K score {score} period={period} "
                      f"-- not results, ignoring.")

    if sent:
        watch["hit"] = True
        state[key] = watch
    return True


def poll() -> None:
    """The detection loop. Exits after LOOP_MINUTES; the workflow restarts it."""
    deadline = datetime.now(timezone.utc) + timedelta(minutes=LOOP_MINUTES)
    cik_map = sec_edgar.load_cik_map()
    etags = {}
    cycles = 0

    while datetime.now(timezone.utc) < deadline:
        state = load_state()
        watches = _watches(state)

        if not watches:
            # Exit rather than idle. Nine holdings reporting quarterly means
            # roughly 36 armed days a year out of ~250 trading days; idling
            # through the other ~215 held a runner for about 11 hours a day
            # to do nothing.
            #
            # Exiting is only safe because arming TRIGGERS this workflow --
            # both the calendar job and the Telegram "earnings for" command
            # dispatch it -- so an on-demand watch starts within seconds
            # instead of waiting for the next hourly run.
            print(f"No armed watches after {cycles} cycles; exiting. "
                  f"Arming will start a fresh watcher.")
            return

        now = now_et()
        changed = False

        for key, watch in list(watches.items()):
            ticker = key.split("::", 1)[1]

            try:
                expires = datetime.fromisoformat(watch["expires"])
            except (KeyError, TypeError, ValueError):
                print(f"[{ticker}] malformed watch, dropping.")
                del state[key]
                changed = True
                continue

            if now >= expires:
                if not watch.get("hit"):
                    send_telegram_message(
                        f"⚠️ *{ticker}*: no earnings filing appeared in "
                        f"{ON_DEMAND_WATCH_HOURS}h. Worth a manual look."
                    )
                del state[key]
                state.pop(f"ew_seen::{ticker}", None)
                changed = True
                continue

            cik = sec_edgar.resolve_cik(ticker, cik_map)
            if not cik:
                print(f"[{ticker}] no CIK; cannot watch filings.")
                continue

            seen = _baseline(state, ticker, cik)
            if seen is None:
                changed = True   # baseline attempt may have mutated state
                continue

            try:
                filings, etags[ticker] = sec_edgar.recent_filings(
                    cik, etag=etags.get(ticker))
            except sec_edgar.FetchError as e:
                # Explicitly NOT treated as "nothing found". A swallowed
                # network error reading as a routine filing is exactly how a
                # missed earnings report would look like normal operation.
                print(f"[{ticker}] poll failed, will retry: {e}")
                continue

            if filings is None:
                continue  # 304 -- nothing changed since last poll

            if _process(state, key, watch, ticker, cik, filings, seen):
                changed = True

        if changed:
            save_state(state)

        cycles += 1
        time.sleep(POLL_SECONDS)

    print(f"Loop finished after {cycles} cycles; the hourly run takes over.")


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "poll"
    if mode == "arm":
        arm()
    elif mode == "poll":
        poll()
    else:
        print(f"Unknown mode {mode!r}; expected 'arm' or 'poll'.")
        sys.exit(2)


if __name__ == "__main__":
    main()
