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

import llm_extract
import sec_edgar
from config import ON_DEMAND_WATCH_HOURS, TICKERS
from earnings_utils import (
    EASTERN,
    arm_earnings_watch,
    classify_holdings_for_date,
    date_str_et,
    fetch_consensus,
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

def _watches(state):
    return {k: v for k, v in state.items() if k.startswith("ew_watch::")}


def arm() -> None:
    """Arm watches for holdings EITHER calendar says report today or tomorrow.

    Finnhub and Nasdaq are both consulted and the results unioned. Knowing a
    report is SCHEDULED is the one thing filings can never tell you, since a
    filing only ever reports what has already happened -- so a calendar is
    unavoidable, and a single one is a single point of failure.

    Two sources make a miss less likely; they do not make it impossible.
    Nothing here detects a company neither calendar lists, and the "earnings
    for <ticker>" command remains the manual override for that.
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
                _store_consensus(state, ticker, day)
                key = f"ew_watch::{ticker.upper()}"
                consensus = state.get(key, {}).get("consensus")
                print(f"[{ticker}] armed ({category}, reports {label})")
                send_telegram_message(
                    f"\U0001F514 *{ticker}* reports earnings {label}. "
                    f"I'm watching their SEC filings and will send results "
                    f"within seconds of them being filed."
                )
                # After the heads-up, so a catch-up alert reads in order.
                if _set_baseline_now(state, ticker, consensus):
                    state[key]["hit"] = True

    if not changed:
        print("No new watches to arm.")
        return

    save_state(state)

    # COMMIT BEFORE DISPATCH, and not only for the reason the listener has.
    #
    # This job used to end by dispatching the watcher and letting the
    # workflow's commit step run afterwards. Both land in the same
    # concurrency group, and the watch lane cancels in progress -- so the
    # dispatch killed THIS JOB before its commit step ran. Every single time.
    #
    # On 2026-08-24 that produced the worst possible shape of failure: the
    # Telegram heads-up went out, so it looked armed, but state.json never
    # recorded the watch. The watcher started, found nothing armed, exited in
    # four seconds, and XPeng's results were never sent. The run shows as
    # "cancelled" rather than "failed", so nothing looked wrong.
    #
    # The jobs now use separate concurrency groups, and committing here means
    # a cancellation after this point costs nothing.
    import repo_commit
    repo_commit.commit_and_push(["state.json"], "Arm earnings watches [skip ci]")

    from workflow_trigger import start_earnings_watcher
    start_earnings_watcher()


def _store_consensus(state, ticker, day):
    """Record the analyst EPS estimate now, so beat/miss costs nothing later.

    Estimates are published before the company reports, unlike Finnhub's
    epsActual, which is the field that lagged the Cerebras release by an
    entire evening. Fetching it at arm time keeps the alert path fast and
    keeps a slow calendar API out of the moment that matters.
    """
    try:
        consensus = fetch_consensus(ticker, day)
    except Exception as e:
        print(f"[{ticker}] consensus lookup failed: {type(e).__name__}: {e}")
        return
    if consensus:
        key = f"ew_watch::{ticker.upper()}"
        if key in state:
            state[key]["consensus"] = consensus
            print(f"[{ticker}] consensus EPS {consensus['eps_estimate']}")


def _accepted_today(filing) -> bool:
    """Was this filing accepted today, Eastern time?

    EDGAR reports acceptanceDateTime with a trailing Z, but the values are
    Eastern -- EDGAR only accepts filings between 06:00 and 22:00 ET, and
    XPeng's 2026-08-24 6-K carries 06:45:59, which is impossible as UTC.
    Treating it as UTC would place every morning filing "yesterday" and defeat
    the whole check below.
    """
    stamp = (filing.get("accepted") or "")[:10]
    return bool(stamp) and stamp == now_et().strftime("%Y-%m-%d")


def _set_baseline_now(state, ticker, consensus=None):
    """Record what already exists -- but never bury a filing already made.

    The baseline marks "everything before this is old". Setting it at arm time
    rather than at first poll closes the gap between the two: a company armed
    at 06:00 that files at 06:02 and is first polled at 06:05 would otherwise
    have had its filing swallowed and treated as already seen.

    That is not enough on its own, because arming itself can be late. On
    2026-08-24 XPeng filed at 06:46 ET and the arm job did not run until
    06:57 -- so the earnings 6-K was the newest filing at arm time and would
    have become the baseline. No alert, and a log that looked entirely normal:
    the exact failure this function exists to prevent, one level up.

    So before baselining, look at what arrived TODAY. If results are already
    out, report them now rather than marking them old.
    """
    cik_map = sec_edgar.load_cik_map()
    cik = sec_edgar.resolve_cik(ticker, cik_map)
    if not cik:
        return False
    try:
        filings, _ = sec_edgar.recent_filings(cik)
    except sec_edgar.FetchError as e:
        # Leaving the key unset is the safe failure: the first poll will set
        # it instead, which is the old behaviour rather than a worse one.
        print(f"[{ticker}] baseline at arm time failed ({e}); poll will set it.")
        return False

    state[f"ew_seen::{ticker}"] = filings[0]["accession"] if filings else ""

    # Oldest first, so a company filing twice in a morning arrives in order.
    caught = False
    for filing in reversed([f for f in filings if _accepted_today(f)]):
        if sec_edgar.is_domestic_earnings(filing):
            print(f"[{ticker}] CATCH-UP: earnings already filed today at "
                  f"{filing.get('accepted')}")
            _report(ticker, cik, filing, "8-K item 2.02 (already filed)", consensus)
            caught = True
        elif filing["form"] == "6-K":
            try:
                score, period, _ = sec_edgar.score_filing(cik, filing["accession"])
            except sec_edgar.FetchError as e:
                print(f"[{ticker}] catch-up scoring failed: {e}")
                continue
            if sec_edgar.is_foreign_earnings(score, period):
                print(f"[{ticker}] CATCH-UP: earnings already filed today at "
                      f"{filing.get('accepted')}")
                _report(ticker, cik, filing,
                        f"6-K (score {score}, already filed)", consensus)
                caught = True
    return caught


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


_METRIC_ORDER = (
    ("period", "Period"),
    ("revenue", "Revenue"),
    ("revenue_yoy", "Revenue YoY"),
    ("eps_gaap", "EPS (GAAP)"),
    ("eps_adjusted", "EPS (adj.)"),
    ("net_income", "Net income"),
    ("gross_margin", "Gross margin"),
    ("operating_income", "Operating income"),
    ("operating_margin", "Operating margin"),
    ("adjusted_ebitda", "Adj. EBITDA"),
    ("free_cash_flow", "Free cash flow"),
)


def build_metrics_message(ticker, text, consensus):
    """The follow-up message: figures, beat/miss, guidance, and a quote.

    Order is deliberate. Beat/miss leads because it is the most price-relevant
    single line; guidance follows because it frequently moves the stock more
    than the quarter itself; the company's own words come last as a check on
    everything above.

    The verbatim quote is always included, even when extraction succeeds.
    Structured figures are convenient but they are a machine's reading of a
    document; the quote is what the company actually wrote, and it costs a few
    lines to make the difference visible rather than invisible.
    """
    metrics = llm_extract.extract_metrics(text, ticker)
    lines = []

    if metrics:
        verdict = llm_extract.compare_to_consensus(metrics, consensus or {})
        if verdict:
            lines.append(f"*{escape_markdown(verdict)}*")
        elif consensus and consensus.get("eps_estimate") is not None:
            lines.append(f"_Consensus EPS was {consensus['eps_estimate']}_")

        if metrics.get("headline"):
            lines.append(escape_markdown(metrics["headline"]))

        figures = [f"{label}: {metrics[key]}"
                   for key, label in _METRIC_ORDER if metrics.get(key)]
        if figures:
            lines.append("")
            lines.extend(escape_markdown(f) for f in figures)

        if metrics.get("guidance"):
            lines.append("")
            lines.append(f"*Guidance*: {escape_markdown(metrics['guidance'])}")

    quote = llm_extract.highlights(text)
    if quote:
        lines.append("")
        lines.append("_From the release:_")
        lines.append(escape_markdown(quote))

    if not lines:
        return ""
    return f"*{ticker}*\n" + "\n".join(lines)


def _report(ticker, cik, filing, kind, consensus=None):
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

    message = build_metrics_message(ticker, text, consensus)
    if message:
        send_telegram_message(message)

    # The number that decides whether POLL_SECONDS is worth tightening. It
    # can only be measured against a real filing, so it is logged every time.
    try:
        # EDGAR writes a trailing Z but the value is EASTERN, not UTC -- it
        # only accepts filings 06:00-22:00 ET, and 06:45:59 is impossible as
        # UTC. Reading it as UTC inflated this figure by four hours, which
        # would have made a 15-second detection look like an hour's lag.
        filed_at = datetime.fromisoformat(
            accepted.replace("Z", "").replace("+00:00", "")).replace(tzinfo=EASTERN)
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
            _report(ticker, cik, filing, "8-K item 2.02", watch.get("consensus"))
            sent = True
        elif filing["form"] == "6-K":
            try:
                score, period, _ = sec_edgar.score_filing(cik, filing["accession"])
            except sec_edgar.FetchError as e:
                print(f"[{ticker}] 6-K scoring failed, skipping: {e}")
                continue
            if sec_edgar.is_foreign_earnings(score, period):
                _report(ticker, cik, filing, f"6-K (score {score})", watch.get("consensus"))
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
