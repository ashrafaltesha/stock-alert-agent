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
import early_signal
import health
import heartbeat
import sec_edgar
from config import ON_DEMAND_WATCH_HOURS, TICKERS
# STDLIB ONLY at module level. classify_holdings_for_date and
# fetch_consensus live in earnings_utils, which imports requests and
# yfinance; they are imported inside arm(), which runs in the job that
# installs them. The poll loop must never reach them -- see timeutil.
from timeutil import EASTERN, arm_earnings_watch, date_str_et, now_et
from state_utils import load_state, save_state
from telegram_utils import escape_markdown, send_telegram_message

# 15 seconds, not 10. The difference in worst-case latency is about five
# seconds; the difference in request volume is roughly a third. Losing SEC
# access would remove the only universal source available, so the trade is
# not close. Revisit once the SEC's own indexing lag is measured -- if that
# turns out to be 45 seconds, nothing below a minute matters anyway.
POLL_SECONDS = 15

# The early wire check runs once every this many poll cycles, not on every
# one. At 15s polling that is roughly a minute, which is fast enough for a
# signal whose whole purpose is to beat a filing that may be hours away, and
# it keeps Google News requests to a handful per armed hour instead of 240.
EARLY_EVERY_N_CYCLES = 4

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
# Same reasoning as the Telegram listener: the hourly cron is unreliable
# enough that a 62-minute loop leaves real gaps. Idle watchers still exit
# within seconds, so a long ceiling costs nothing on a day with nothing
# armed -- it only matters on the day something is.
LOOP_MINUTES = 330

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
    # Imported here, not at module scope: these reach requests and yfinance,
    # and arm() only ever runs in the job that installs them.
    from earnings_utils import classify_holdings_for_date

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

    # Calendars are advisory; this is the backstop that needs none.
    if _arm_overdue_holdings(state):
        changed = True

    if not changed:
        print("No new watches to arm.")
        # Still alive: "nothing reports today" is a normal, healthy answer,
        # and must not look the same as the job never running.
        heartbeat.ping(heartbeat.EARNINGS_ARM)
        health.record(state, "earnings_arm")
        save_state(state)
        return

    health.record(state, "earnings_arm")
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

    # Arming is the single point of failure for the whole earnings path: no
    # watch armed means detection never runs, however good it is. This is the
    # thing most worth knowing has stopped.
    heartbeat.ping(heartbeat.EARNINGS_ARM)


# A quarter is ~91 days. Past this a company is overdue and the calendars have
# had every chance to say so.
STALE_EARNINGS_DAYS = 100

# How many recent filings to inspect when establishing the last earnings date.
# Bounded because 6-K classification costs a document fetch each.
BOOTSTRAP_SCAN = 12
BOOTSTRAP_SCORE_LIMIT = 4

LAST_EARNINGS_KEY = "ew_last_earnings::{ticker}"


def _note_earnings_date(state, ticker, filed) -> None:
    if filed:
        state[LAST_EARNINGS_KEY.format(ticker=ticker.upper())] = filed


def _last_earnings_date(state, ticker, cik):
    """When this company last filed results, learning it once if unknown.

    Kept in state so the staleness check below is free on every later run.
    Bootstrapping costs a handful of document fetches per ticker, once.
    """
    key = LAST_EARNINGS_KEY.format(ticker=ticker.upper())
    if key in state:
        return state[key]

    try:
        filings, _ = sec_edgar.recent_filings(cik)
    except sec_edgar.FetchError as e:
        print(f"[{ticker}] could not read filing history: {e}")
        return None

    scored = 0
    for filing in (filings or [])[:BOOTSTRAP_SCAN]:
        if sec_edgar.is_domestic_earnings(filing):
            state[key] = filing.get("filed")
            return state[key]
        if filing["form"] == "6-K" and scored < BOOTSTRAP_SCORE_LIMIT:
            scored += 1
            try:
                score, period, _ = sec_edgar.score_filing(cik, filing["accession"])
            except sec_edgar.FetchError:
                continue
            if sec_edgar.is_foreign_earnings(score, period):
                state[key] = filing.get("filed")
                return state[key]
    return None


def _arm_overdue_holdings(state) -> bool:
    """Arm anything that has not reported in a quarter, whatever the calendars say.

    Arming is the single point of failure in the earnings path: no watch armed
    means detection never runs, however good it is. Two calendars make a miss
    less likely and not impossible, and their failures cluster on exactly the
    companies worth watching -- recent IPOs and foreign issuers.

    This needs no calendar at all. A company that last reported more than a
    hundred days ago is due, and arming it costs a watch that expires quietly
    in 24 hours. That asymmetry is the whole argument: a wasted watch costs a
    few hundred requests, a missed one costs the alert.
    """
    cik_map = sec_edgar.load_cik_map()
    today = now_et().date()
    changed = False

    for ticker in TICKERS:
        key = f"ew_watch::{ticker.upper()}"
        if key in state:
            continue                      # already armed by a calendar
        cik = sec_edgar.resolve_cik(ticker, cik_map)
        if not cik:
            continue

        last = _last_earnings_date(state, ticker, cik)
        changed = True                    # the lookup itself may have written
        if not last:
            print(f"[{ticker}] no earnings filing found in recent history; "
                  f"not arming on a guess.")
            continue

        try:
            age = (today - datetime.strptime(last, "%Y-%m-%d").date()).days
        except (TypeError, ValueError):
            continue
        if age < STALE_EARNINGS_DAYS:
            continue

        if arm_earnings_watch(state, ticker, ON_DEMAND_WATCH_HOURS):
            print(f"[{ticker}] OVERDUE: last reported {last} ({age}d ago); "
                  f"arming without a calendar.")
            send_telegram_message(
                f"\U0001F514 *{ticker}* last reported {last}, {age} days ago. "
                f"Neither calendar lists it, so I'm watching its SEC filings "
                f"anyway for the next {ON_DEMAND_WATCH_HOURS}h."
            )
            if _set_baseline_now(state, ticker, None):
                state[key]["hit"] = True
    return changed


def _store_consensus(state, ticker, day):
    """Record the analyst EPS estimate now, so beat/miss costs nothing later.

    Estimates are published before the company reports, unlike Finnhub's
    epsActual, which is the field that lagged the Cerebras release by an
    entire evening. Fetching it at arm time keeps the alert path fast and
    keeps a slow calendar API out of the moment that matters.
    """
    try:
        # Local import: _store_consensus is only reached from arm().
        from earnings_utils import fetch_consensus
        consensus = fetch_consensus(ticker, day)
    except Exception as e:
        print(f"[{ticker}] consensus lookup failed: {type(e).__name__}: {e}")
        return
    if consensus:
        key = f"ew_watch::{ticker.upper()}"
        if key in state:
            state[key]["consensus"] = consensus
            print(f"[{ticker}] consensus EPS {consensus['eps_estimate']}")


# How far back a catch-up will look. Bounded because the mark below is
# permanent: the first time a ticker is ever armed there is no mark, and
# without a limit the "everything newer" rule would mean "every earnings
# release the company has ever filed".
CATCHUP_MAX_DAYS = 4

# The high-water mark: the newest filing this bot has actually EVALUATED for
# a ticker, kept forever rather than deleted when a watch expires.
#
# ew_seen:: cannot serve this purpose. It is the per-watch baseline and is
# deleted when the watch resolves, so each new watch starts from "whatever is
# newest right now" -- which is precisely how a filing that arrived before
# arming gets marked old without ever being read.
MARK_KEY = "ew_mark::{ticker}"


def _accepted_et(filing):
    """Acceptance time as a real datetime.

    EDGAR writes a trailing Z but the values are EASTERN -- it only accepts
    filings between 06:00 and 22:00 ET, and XPeng's 2026-08-24 6-K carries
    06:45:59, which is impossible as UTC. Reading these as UTC shifts every
    morning filing onto the previous day.
    """
    raw = (filing.get("accepted") or "").replace("Z", "").replace("+00:00", "")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=EASTERN)
    except ValueError:
        return None


def _unevaluated(filings, mark):
    """Filings newer than the mark, oldest first, bounded by age.

    Newest-first input; `filings` is ordered that way by the SEC. Stopping at
    the mark rather than filtering on a date is the whole point: it does not
    matter whether arming is eleven minutes late or eleven hours, or that the
    filing landed at 21:00 last night when no watcher was running. Anything
    not yet read is still not yet read.
    """
    cutoff = now_et() - timedelta(days=CATCHUP_MAX_DAYS)
    fresh = []
    for f in filings:
        if mark and f["accession"] == mark:
            break
        when = _accepted_et(f)
        if when and when < cutoff:
            # Older than the window. Everything past this is older still.
            break
        fresh.append(f)
    return list(reversed(fresh))


def _catch_up(state, ticker, cik, filings, consensus=None):
    """Report any earnings filing that arrived while nothing was watching.

    This is the safety net under a system whose weakest link is knowing WHEN
    to watch. Arming depends on a calendar and on GitHub running a scheduled
    job on time; on 2026-08-24 the job ran 11 minutes after XPeng had already
    filed, and the filing became the baseline instead of the alert.
    """
    mark = state.get(MARK_KEY.format(ticker=ticker))
    pending = _unevaluated(filings, mark)

    if mark is None:
        # First sight of this ticker. Record where we are and report nothing:
        # a brand-new watch should not open with last quarter's results.
        state[MARK_KEY.format(ticker=ticker)] = filings[0]["accession"] if filings else ""
        print(f"[{ticker}] first watch; mark set, no catch-up.")
        return False

    caught = False
    for filing in pending:
        kind = None
        if sec_edgar.is_domestic_earnings(filing):
            kind = "8-K item 2.02"
        elif filing["form"] == "6-K":
            try:
                score, period, _ = sec_edgar.score_filing(cik, filing["accession"])
            except sec_edgar.FetchError as e:
                # Not evaluated, so do NOT advance the mark past it.
                print(f"[{ticker}] catch-up scoring failed, leaving unread: {e}")
                return caught
            if sec_edgar.is_foreign_earnings(score, period):
                kind = f"6-K (score {score})"
        if kind:
            print(f"[{ticker}] CATCH-UP: filed {filing.get('accepted')} "
                  f"before anything was watching")
            _report(ticker, cik, filing, f"{kind}, filed earlier", consensus)
            caught = True
        state[MARK_KEY.format(ticker=ticker)] = filing["accession"]

    if filings:
        state[MARK_KEY.format(ticker=ticker)] = filings[0]["accession"]
    return caught


def _set_baseline_now(state, ticker, consensus=None):
    """Record what already exists -- but never bury a filing not yet read.

    The baseline marks "everything before this is old", and setting it at arm
    time rather than at first poll closes the gap between the two.

    That is not enough on its own, because arming itself can be late or can
    not happen at all. So before baselining, hand off to _catch_up(), which
    compares against a permanent per-ticker mark rather than against the clock.
    """
    cik_map = sec_edgar.load_cik_map()
    cik = sec_edgar.resolve_cik(ticker, cik_map)
    if not cik:
        return False
    try:
        filings, _ = sec_edgar.recent_filings(cik)
    except sec_edgar.FetchError as e:
        # Leaving the keys unset is the safe failure: the first poll will set
        # the baseline instead, which is the old behaviour rather than a
        # worse one.
        print(f"[{ticker}] baseline at arm time failed ({e}); poll will set it.")
        return False

    caught = _catch_up(state, ticker, cik, filings, consensus)
    state[f"ew_seen::{ticker}"] = filings[0]["accession"] if filings else ""
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

    if metrics and metrics is not llm_extract.NO_PROVIDER:
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
            lines.append("*Figures*")
            lines.extend(escape_markdown(f) for f in figures)

        if metrics.get("guidance"):
            lines.append("")
            lines.append("*Guidance*")
            lines.append(escape_markdown(metrics["guidance"]))
    else:
        # Say WHICH failure this was. Naming it turns a shrug into an action:
        # a missing key is a one-line workflow fix, a provider error is not.
        if metrics is llm_extract.NO_PROVIDER:
            lines.append("_No model key configured in this workflow, so no "
                         "figures were parsed. Quoting the release instead._")
        else:
            lines.append("_The model call failed, so no figures were parsed. "
                         "Quoting the release instead._")

    quote = llm_extract.highlights(text)
    if quote:
        lines.append("")
        lines.append("*From the release*")
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
    # The permanent mark advances with the per-watch baseline, so a filing
    # handled live here is never re-reported by a later catch-up.
    state[MARK_KEY.format(ticker=ticker)] = filings[0]["accession"]
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
        health.record_alert(state, "earnings")
        _note_earnings_date(state, ticker, filings[0].get("filed"))
    return True


def _check_early_signal(state, key, watch, ticker, filings, cycles) -> bool:
    """Wire-service heads-up while waiting on a lagging foreign filing.

    Deliberately narrow. It fires at most once per watch, only for foreign
    private issuers, only while the watch is armed, and only on a results
    headline from an outlet that republishes company releases.

    It does NOT resolve the watch. `hit` is left alone, so the watcher keeps
    polling EDGAR and still sends the figures when the filing lands -- this
    is a heads-up, not a substitute for the document.
    """
    if watch.get("early_sent") or watch.get("hit"):
        return False
    if cycles % EARLY_EVERY_N_CYCLES:
        return False
    if not sec_edgar.is_foreign_issuer(filings):
        return False   # domestic issuers file with the release; nothing to gain

    hit = early_signal.find_results_headline(
        ticker, state.get(f"company_name::{ticker}", ""))
    if not hit:
        return False

    print(f"[{ticker}] EARLY SIGNAL: {hit['source']} -- {hit['title'][:80]}")
    send_telegram_message(
        f"\u26A1 *{ticker} results appear to be out*\n"
        f"{escape_markdown(hit['title'])}\n"
        f"_{escape_markdown(hit['source'])}_\n"
        f"{hit['link']}\n\n"
        f"_Headline only -- their SEC filing hasn't landed yet. "
        f"I'll send the figures when it does._"
    )
    watch["early_sent"] = True
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
        # The watcher only reaches here with something armed, which is what
        # makes this a meaningful signal rather than a clock tick.
        health.record(state, "earnings_watch")
        changed = True

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
                continue

            # Nothing on EDGAR yet. For a foreign private issuer that may
            # simply mean the filing has not arrived, so look for the wire.
            if _check_early_signal(state, key, watch, ticker, filings, cycles):
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
