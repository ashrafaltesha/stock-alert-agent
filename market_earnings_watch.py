"""Ranks a day's earnings reporters, for the "earnings today" command.

This module used to WATCH those companies too, polling Finnhub's epsActual in
a time.sleep loop that could hold a GitHub runner for three hours. That was
removed along with its workflow: detection now happens in earnings_watch.py
against SEC filings, and watching ten companies you don't own was mostly
noise once "earnings for <ticker>" worked properly for any symbol.

What remains is the ranking used by "earnings today":

  * the largest reporters by market cap, from Nasdaq's calendar (Finnhub's
    free tier doesn't include market cap, which is why the two sources are
    both still here)
  * a few more by analyst attention, among that day's reporters not already
    in the market-cap list
"""

from config import (
    TOP_N_EARNINGS,
    TOP_N_ANALYST_ATTENTION,
    ANALYST_LOOKUP_POOL_SIZE,
)
from earnings_utils import (
    date_str_et,
    fetch_earnings_calendar,
    parse_market_cap,
    get_ticker_info,
)


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
