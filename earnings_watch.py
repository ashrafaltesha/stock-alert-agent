"""Per-holding earnings reminders + release-detection watcher.

Three modes (each its own GitHub Actions workflow -- see
.github/workflows/earnings_bmo_reminder.yml,
earnings_watch_premarket.yml, and earnings_watch_afterhours.yml):

  bmo_reminder
    Runs once daily around 6pm ET. Checks TOMORROW's Nasdaq earnings
    calendar for any holding reporting before market open, and sends a
    heads-up reminder for each.

  premarket_watch
    Scheduled repeatedly across the pre-market window. Runs before
    EARNINGS_BMO_POLL_START_ET exit immediately; from then on each run
    checks TODAY's calendar for before-market-open holdings (plus any
    "time not supplied" holdings, as a safety net) and makes ONE pass
    looking for the release, sending a beat/miss summary the moment it
    appears.

  afterhours_watch
    Same shape for the after-close window: runs at or after
    EARNINGS_AMC_REMINDER_TIME_ET send the "reports after close today"
    reminder, and runs at or after EARNINGS_AMC_POLL_START_ET make one
    detection pass each.

Neither mode sleeps on the runner any more. Repetition comes from the
workflow cron firing across the window, with progress kept in state.json
so successive short runs continue where the previous one stopped. A run
on a day with nothing reporting exits almost immediately.

The "still not detected" give-up notice is now based on how long the
poll window has been open (recorded in state), not elapsed time inside a
single process -- see EARNINGS_POLL_TIMEOUT_MINUTES (config.py).

See earnings_summary.py for the release-detection method and the data-source
caveats (why "beat/missed" is EPS-based, why revenue can lag a bit).
"""

import sys

from config import (
    TICKERS,
    EARNINGS_BMO_REMINDER_TIME_ET,
    EARNINGS_BMO_POLL_START_ET,
    EARNINGS_AMC_REMINDER_TIME_ET,
    EARNINGS_AMC_POLL_START_ET,
)
from earnings_utils import date_str_et, classify_holdings_for_date, now_et, TEST_MODE
from earnings_summary import (
    check_releases_once,
    mark_poll_window_open,
    maybe_give_up,
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


def run_bmo_reminder() -> None:
    tomorrow = date_str_et(1)
    classification = classify_holdings_for_date(tomorrow, TICKERS)
    bmo_tickers = [t for t, cat in classification.items() if cat == "bmo"]
    if not bmo_tickers:
        print(f"No holdings reporting before market open on {tomorrow}.")
        return

    state = load_state()
    to_remind = [t for t in bmo_tickers if not state.get(f"ew_bmo_reminder_sent::{t}::{tomorrow}")]
    if not to_remind:
        print("BMO reminder(s) already sent for tomorrow.")
        return

    if _too_early_for("BMO reminder", EARNINGS_BMO_REMINDER_TIME_ET):
        return

    for ticker in to_remind:
        send_telegram_message(
            f"\U0001F514 *{ticker}* reports earnings tomorrow *before market open*. "
            f"I'll start checking for the release around {EARNINGS_BMO_POLL_START_ET} ET "
            f"and send a summary as soon as it's out."
        )
        state[f"ew_bmo_reminder_sent::{ticker}::{tomorrow}"] = True
    save_state(state)


def run_premarket_watch() -> None:
    today = date_str_et(0)
    classification = classify_holdings_for_date(today, TICKERS)
    bmo_tickers = [t for t, cat in classification.items() if cat == "bmo"]
    # "Time not supplied" tickers get swept into both the premarket and
    # afterhours watch as a safety net, since we don't know their timing.
    unsupplied = [t for t, cat in classification.items() if cat == "unsupplied"]

    if not bmo_tickers and not unsupplied:
        print(f"No holdings reporting before market open (or unknown-time) today ({today}).")
        return

    state = load_state()

    if unsupplied:
        for ticker in unsupplied:
            notice_key = f"ew_unsupplied_notice_sent::{ticker}::{today}"
            if not state.get(notice_key):
                send_telegram_message(
                    f"\U0001F514 *{ticker}* reports earnings today, but Nasdaq's calendar "
                    f"doesn't specify before-open or after-close. I'll monitor both windows "
                    f"and send a summary as soon as the release is detected."
                )
                state[notice_key] = True
        save_state(state)

    watch_list = [
        t for t in (bmo_tickers + unsupplied)
        if not state.get(f"ew_summary_sent::{t}::{today}")
    ]
    if not watch_list:
        print("Already have summaries for all of today's before-open reporters.")
        return

    if _too_early_for("Pre-market watch", EARNINGS_BMO_POLL_START_ET):
        return

    mark_poll_window_open("premarket", today, state)
    pending = check_releases_once(watch_list, today, state)
    if pending:
        print(f"Still awaiting release for: {', '.join(pending)}")
    maybe_give_up(pending, "premarket", today, state)


def run_afterhours_watch() -> None:
    today = date_str_et(0)
    classification = classify_holdings_for_date(today, TICKERS)
    amc_tickers = [t for t, cat in classification.items() if cat == "amc"]
    unsupplied = [t for t, cat in classification.items() if cat == "unsupplied"]

    if not amc_tickers and not unsupplied:
        print(f"No holdings reporting after market close (or unknown-time) today ({today}).")
        return

    state = load_state()

    to_remind = [t for t in amc_tickers if not state.get(f"ew_amc_reminder_sent::{t}::{today}")]
    if to_remind:
        if _too_early_for("AMC reminder", EARNINGS_AMC_REMINDER_TIME_ET):
            return

        for ticker in to_remind:
            send_telegram_message(
                f"\U0001F514 *{ticker}* reports earnings today *after market close* "
                f"(expected ~{EARNINGS_AMC_POLL_START_ET} ET). I'll start checking then "
                f"and send a summary as soon as it's out."
            )
            state[f"ew_amc_reminder_sent::{ticker}::{today}"] = True
        save_state(state)

    watch_list = [
        t for t in (amc_tickers + unsupplied)
        if not state.get(f"ew_summary_sent::{t}::{today}")
    ]
    if not watch_list:
        print("Already have summaries for all of today's after-close reporters.")
        return

    if _too_early_for("After-hours watch", EARNINGS_AMC_POLL_START_ET):
        return

    mark_poll_window_open("afterhours", today, state)
    pending = check_releases_once(watch_list, today, state)
    if pending:
        print(f"Still awaiting release for: {', '.join(pending)}")
    maybe_give_up(pending, "afterhours", today, state)


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
