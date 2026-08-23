"""Arms earnings watches for the stocks you own.

This file does NOT detect releases. It only decides which holdings are worth
watching and creates the watch record; detection happens in
telegram_commands.check_on_demand_earnings(), which already runs every couple
of minutes and reads the company's own investor-relations feed or page.

That split is deliberate and was a bug fix. This file used to poll Finnhub's
epsActual field itself, which meant the stocks you actually own were on a
weaker detector than any ticker you asked about by hand. On 2026-08-12
Cerebras published its results minutes after the close while Finnhub stayed
empty all evening, and no alert went out. Now both paths create the same
ew_watch:: record and run through the same detection.

Three modes, each its own GitHub Actions workflow (see
.github/workflows/earnings_bmo_reminder.yml, earnings_watch_premarket.yml
and earnings_watch_afterhours.yml):

  bmo_reminder
    Evening. Arms tomorrow's before-open reporters. Arming the night before
    is the point -- the watch runs for ON_DEMAND_WATCH_HOURS and the morning
    poll window opens at 06:00 ET, so a pre-market release is caught without
    anyone being awake for it.

  premarket_watch
    Morning. A safety net for the same set, catching companies the calendar
    only listed overnight, or an evening run that didn't fire. Arming twice
    is harmless: an existing watch is left untouched rather than reset, since
    its record holds the list of articles already sent.

  afterhours_watch
    Afternoon. Arms today's after-close reporters.

Holdings with no stated timing ("unsupplied") are swept into both the morning
and afternoon passes, since there's no way to tell which window applies.

Finnhub supplies the calendar -- which companies report and roughly when --
and nothing else. That is the one thing a feed can never tell you, since a
feed only reports what has already happened.

No mode sleeps on the runner. Repetition comes from the workflow cron firing
across the window, and a day with nothing reporting exits almost immediately.
"""

import sys

from config import (
    TICKERS,
    EARNINGS_BMO_REMINDER_TIME_ET,
    EARNINGS_AMC_REMINDER_TIME_ET,
    ON_DEMAND_WATCH_HOURS,
    ON_DEMAND_POLL_WINDOWS_ET,
)
from earnings_utils import (
    date_str_et,
    classify_holdings_for_date,
    now_et,
    arm_earnings_watch,
    TEST_MODE,
)


def _too_early_for(label: str, target_et: str) -> bool:
    """True if the given ET time hasn't arrived yet today.

    This replaces sleep_until_et(). Instead of holding a runner idle until
    the window opens -- which risked losing all progress if the job was
    evicted, and would stall the shared repo-write concurrency group that
    the one-minute pollers also use -- the run simply exits early and a
    later scheduled run picks the work up. The workflow cron must
    therefore fire repeatedly across the window rather than once at the
    start of it.
    """
    if TEST_MODE:
        return False
    now = now_et()
    hour, minute = (int(part) for part in target_et.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < target:
        print(
            f"{label}: {target_et} ET hasn't arrived yet (now "
            f"{now.strftime('%H:%M')} ET). Exiting; a later run handles it."
        )
        return True
    return False
from telegram_utils import send_telegram_message
from state_utils import load_state, save_state


def _arm_and_notify(tickers, when_label, state, notice_key_suffix):
    """Arm a watch for each ticker and send one heads-up per ticker per day.

    Arming is all this does. The actual detection happens in
    telegram_commands.check_on_demand_earnings(), which already runs every
    couple of minutes and knows how to read a company's IR page. Previously
    this file did its own polling against Finnhub's epsActual -- the source
    that stayed empty for hours after Cerebras reported -- so your holdings
    were on a weaker detector than any ticker you asked about by hand.
    """
    armed_any = False
    for ticker in tickers:
        newly = arm_earnings_watch(state, ticker, ON_DEMAND_WATCH_HOURS)
        notice_key = f"ew_notice_sent::{ticker}::{notice_key_suffix}"
        if not state.get(notice_key):
            windows = ", ".join(f"{a}-{b}" for a, b in ON_DEMAND_POLL_WINDOWS_ET)
            send_telegram_message(
                f"\U0001F514 *{ticker}* reports earnings {when_label}. "
                f"I'll check their investor-relations page every couple of "
                f"minutes during {windows} ET and send you anything they "
                f"publish that day, with a link."
            )
            state[notice_key] = True
            armed_any = True
        elif newly:
            armed_any = True
        print(f"[{ticker}] watch {'armed' if newly else 'already active'}.")
    return armed_any


def run_bmo_reminder() -> None:
    """Evening job: arm tomorrow's before-open reporters.

    Arming the night before is the whole point. The watch lasts
    ON_DEMAND_WATCH_HOURS, and the morning poll window opens at 06:00 ET, so
    a company reporting pre-market is covered without anyone being awake.
    """
    tomorrow = date_str_et(1)
    classification = classify_holdings_for_date(tomorrow, TICKERS)
    bmo_tickers = [t for t, cat in classification.items() if cat == "bmo"]
    if not bmo_tickers:
        print(f"No holdings reporting before market open on {tomorrow}.")
        return

    if _too_early_for("BMO reminder", EARNINGS_BMO_REMINDER_TIME_ET):
        return

    state = load_state()
    if _arm_and_notify(bmo_tickers, "tomorrow *before market open*", state, tomorrow):
        save_state(state)


def run_premarket_watch() -> None:
    """Morning job: a safety net for before-open reporters.

    The evening run should already have armed these. This catches the cases
    it couldn't: a calendar that only listed the company overnight, or an
    evening run that didn't fire. Arming twice is harmless -- an existing
    watch is left untouched.
    """
    today = date_str_et(0)
    classification = classify_holdings_for_date(today, TICKERS)
    # "Time not supplied" tickers are swept into BOTH windows, since we don't
    # know whether they report before the open or after the close.
    targets = [t for t, cat in classification.items() if cat in ("bmo", "unsupplied")]
    if not targets:
        print(f"No holdings reporting before market open (or unknown-time) today ({today}).")
        return

    state = load_state()
    if _arm_and_notify(targets, "today", state, today):
        save_state(state)


def run_afterhours_watch() -> None:
    """Afternoon job: arm today's after-close reporters."""
    today = date_str_et(0)
    classification = classify_holdings_for_date(today, TICKERS)
    targets = [t for t, cat in classification.items() if cat in ("amc", "unsupplied")]
    if not targets:
        print(f"No holdings reporting after market close (or unknown-time) today ({today}).")
        return

    if _too_early_for("After-hours watch", EARNINGS_AMC_REMINDER_TIME_ET):
        return

    state = load_state()
    if _arm_and_notify(targets, "today *after market close*", state, today):
        save_state(state)


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in ("bmo_reminder", "premarket_watch", "afterhours_watch"):
        print("Usage: python earnings_watch.py {bmo_reminder|premarket_watch|afterhours_watch} [--test]")
        sys.exit(1)

    mode = sys.argv[1]
    if TEST_MODE:
        print(f"[TEST MODE] running '{mode}' with sleeps skipped and a short poll timeout.")

    if mode == "bmo_reminder":
        run_bmo_reminder()
    elif mode == "premarket_watch":
        run_premarket_watch()
    elif mode == "afterhours_watch":
        run_afterhours_watch()


if __name__ == "__main__":
    main()
