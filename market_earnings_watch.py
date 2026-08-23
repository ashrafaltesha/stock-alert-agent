"""Market-wide (not-your-holdings) earnings watcher.

NOTE: this no longer runs automatically. The old daily 3:55pm list-send +
auto-poll-everything behavior has been replaced by on-demand Telegram
commands handled in telegram_commands.py:
  "earnings today"            -- sends the day's list immediately.
  "earnings for TICKER, ..."  -- polls those specific tickers starting at
                                 MARKET_EARNINGS_POLL_START_ET ET.

select_top_reporters() and format_list_line() below are imported by
telegram_commands.py's "earnings today" handler. main() below (the old
full list-send + poll-everything flow) is kept only for manual testing via
workflow_dispatch (see .github/workflows/earnings_market_watch.yml, which
no longer has a schedule trigger).

At ~3:55pm ET (config.MARKET_EARNINGS_LIST_TIME_ET), if manually run:
  Sends two heads-up lists --
    - Top TOP_N_EARNINGS companies reporting earnings today, by market cap.
    - Up to TOP_N_ANALYST_ATTENTION additional companies (not already in the
      market-cap list) getting the most analyst attention among today's
      reporters.

At ~4:00pm ET (config.MARKET_EARNINGS_POLL_START_ET), if manually run:
  Polls roughly once a minute for each of those companies' earnings release
  (Finnhub earnings history -- see earnings_summary.py; note this is NOT
  how per-holding watches work any more, those read the company's own IR
  page via telegram_commands.check_on_demand_earnings),
  sending an individual beat/miss + revenue/EPS + QoQ/YoY summary as soon as
  each is detected, up to EARNINGS_POLL_TIMEOUT_MINUTES.

This is independent of your personal holdings, which get their own
reminder + summary flow via earnings_watch.py.
"""

from config import (
    TOP_N_EARNINGS,
    TOP_N_ANALYST_ATTENTION,
    ANALYST_LOOKUP_POOL_SIZE,
    MARKET_EARNINGS_LIST_TIME_ET,
    MARKET_EARNINGS_POLL_START_ET,
)
from earnings_utils import (
    date_str_et,
    sleep_until_et,
    fetch_earnings_calendar,
    parse_market_cap,
    get_ticker_info,
)
from earnings_summary import poll_for_releases
from telegram_utils import send_telegram_message
from state_utils import load_state, save_state


def select_top_reporters(date_str: str) -> tuple[list[dict], list[dict]]:
    """Returns (top_by_market_cap, top_by_analyst_attention)."""
    rows = fetch_earnings_calendar(date_str)
    if not rows:
        return [], []

    by_cap = sorted(rows, key=lambda r: parse_market_cap(r.get("marketCap", "")), reverse=True)
    top_cap = by_cap[:TOP_N_EARNINGS]
    top_cap_symbols = {r.get("symbol") for r in top_cap}

    # Only look up analyst coverage for a bounded candidate pool (largest-cap
    # reporters) to keep this fast on busy earnings days.
    candidate_pool = by_cap[:ANALYST_LOOKUP_POOL_SIZE]
    info_cache = {}
    for row in candidate_pool:
        symbol = row.get("symbol")
        if symbol:
            info_cache[symbol] = get_ticker_info(symbol)

    by_analyst = sorted(
        (r for r in candidate_pool if r.get("symbol") not in top_cap_symbols),
        key=lambda r: info_cache.get(r.get("symbol"), {}).get("numberOfAnalystOpinions") or 0,
        reverse=True,
    )
    top_analyst = [
        r for r in by_analyst
        if info_cache.get(r.get("symbol"), {}).get("numberOfAnalystOpinions")
    ][:TOP_N_ANALYST_ATTENTION]

    return top_cap, top_analyst


def format_list_line(row: dict) -> str:
    symbol = row.get("symbol", "?")
    name = row.get("name", symbol)
    timing = row.get("time", "")
    return f"• *{symbol}* ({name}) — {timing}"


def send_lists(top_cap: list[dict], top_analyst: list[dict], date_str: str, state: dict) -> None:
    cap_key = f"mkt_list_sent::cap::{date_str}"
    if top_cap and not state.get(cap_key):
        lines = [f"\U0001F4CB *Top {len(top_cap)} market-wide earnings today* ({date_str}):"]
        lines += [format_list_line(r) for r in top_cap]
        lines.append(f"\nI'll check for each release starting ~{MARKET_EARNINGS_POLL_START_ET} ET "
                      f"and send a summary as soon as it's out.")
        send_telegram_message("\n".join(lines))
        state[cap_key] = True

    analyst_key = f"mkt_list_sent::analyst::{date_str}"
    if not state.get(analyst_key):
        lines = [f"\U0001F4CB *Most analyst attention today* (up to {TOP_N_ANALYST_ATTENTION}, "
                 f"not already listed above):"]
        if top_analyst:
            lines += [format_list_line(r) for r in top_analyst]
            lines.append(f"\nI'll check for each release starting ~{MARKET_EARNINGS_POLL_START_ET} ET "
                          f"and send a summary as soon as it's out.")
        else:
            lines.append("None found among today's reporters.")
        send_telegram_message("\n".join(lines))
        state[analyst_key] = True


def main() -> None:
    today = date_str_et(0)
    top_cap, top_analyst = select_top_reporters(today)

    if not top_cap and not top_analyst:
        print(f"No earnings calendar data for {today}; nothing to watch.")
        return

    sleep_until_et(MARKET_EARNINGS_LIST_TIME_ET)

    state = load_state()
    send_lists(top_cap, top_analyst, today, state)
    save_state(state)

    watch_symbols = sorted({r.get("symbol") for r in (top_cap + top_analyst) if r.get("symbol")})
    watch_symbols = [s for s in watch_symbols if not state.get(f"ew_summary_sent::{s}::{today}")]
    if not watch_symbols:
        print("Already have summaries for all of today's watched reporters.")
        return

    sleep_until_et(MARKET_EARNINGS_POLL_START_ET)
    poll_for_releases(watch_symbols, today, state)


if __name__ == "__main__":
    main()
