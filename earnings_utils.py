"""Shared helpers for pulling earnings-calendar and ticker data, plus the
sleep-until-a-time helper -- used by earnings_watch.py (per-holding reminder
+ release-detection watcher) and market_earnings_watch.py (market-wide
top-cap / most-analyst-attention watcher).

Two earnings-calendar sources, used for different jobs:
  fetch_earnings_calendar (Nasdaq's public calendar API) -- includes market
    cap, so it's used wherever ranking by market cap matters:
    select_top_reporters() in market_earnings_watch.py.
  fetch_earnings_calendar_finnhub (Finnhub's calendar API, requires
    FINNHUB_API_KEY) -- no market cap, but a documented, stable, ToS-clean
    public API. Used wherever we only need to know WHICH symbols report on a
    given date and WHEN (bmo/amc/unsupplied): classify_holdings_for_date()
    below (used by earnings_watch.py), and telegram_commands.py's
    "earnings for X" not-reporting-today check.

fetch_earnings_history_finnhub -- same Finnhub endpoint, filtered to one
  symbol across a date range instead of one date across all symbols, which
  also surfaces epsActual/revenueActual once Finnhub has them. Used by
  earnings_summary.get_earnings_release() to detect a release (the
  market-wide watcher and the backtester still do; per-holding watches
  no longer do) --
  switched from Yahoo Finance because Yahoo's earnings-dates table lagged
  the real release by hours in practice.
"""

import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import yfinance as yf

from config import FINNHUB_API_KEY

EASTERN = ZoneInfo("America/New_York")

NASDAQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json",
}

# Pass --test on the command line for a fast manual smoke test.
TEST_MODE = "--test" in sys.argv

# Nasdaq's calendar reports one of these three strings per row. We treat
# anything else (or a missing value) the same as "time-not-supplied".
BMO_LABEL = "time-pre-market"
AMC_LABEL = "time-after-hours"
UNSUPPLIED_LABEL = "time-not-supplied"


def now_et() -> datetime:
    return datetime.now(EASTERN)


def date_str_et(offset_days: int = 0) -> str:
    """YYYY-MM-DD for today (offset=0) or a future/past day, in America/New_York."""
    return (now_et() + timedelta(days=offset_days)).strftime("%Y-%m-%d")


def fetch_earnings_calendar(date_str: str) -> list[dict]:
    """Pull a day's earnings calendar from Nasdaq's public API. Includes
    market cap -- use this when ranking by market cap matters (see
    select_top_reporters in market_earnings_watch.py). For simple "is this
    symbol reporting, and when" lookups, prefer fetch_earnings_calendar_finnhub
    below."""
    url = f"https://api.nasdaq.com/api/calendar/earnings?date={date_str}"
    try:
        resp = requests.get(url, headers=NASDAQ_HEADERS, timeout=20)
        resp.raise_for_status()
        rows = resp.json().get("data", {}).get("rows") or []
        return rows
    except Exception as e:
        print(f"Nasdaq earnings calendar fetch failed for {date_str}: {e}")
        return []


def fetch_earnings_calendar_finnhub(date_str: str) -> list[dict]:
    """Pull a single day's earnings calendar from Finnhub's API (requires
    FINNHUB_API_KEY, free tier at finnhub.io/register). Returns rows shaped
    like fetch_earnings_calendar's Nasdaq rows -- {"symbol": ..., "time":
    BMO_LABEL/AMC_LABEL/UNSUPPLIED_LABEL} -- so callers that only need
    symbol + timing (not market cap or company name) can use either source
    interchangeably. Returns [] (not an error) if FINNHUB_API_KEY isn't set,
    so callers should treat an empty result the same as "can't confirm
    either way" rather than "confirmed nothing reporting"."""
    if not FINNHUB_API_KEY:
        print("FINNHUB_API_KEY not set; skipping Finnhub earnings calendar fetch.")
        return []
    url = "https://finnhub.io/api/v1/calendar/earnings"
    params = {"from": date_str, "to": date_str, "token": FINNHUB_API_KEY}
    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        rows = resp.json().get("earningsCalendar") or []
    except Exception as e:
        print(f"Finnhub earnings calendar fetch failed for {date_str}: {e}")
        return []

    hour_to_label = {"bmo": BMO_LABEL, "amc": AMC_LABEL}
    result = []
    for row in rows:
        symbol = row.get("symbol")
        if not symbol:
            continue
        result.append({"symbol": symbol, "time": hour_to_label.get(row.get("hour"), UNSUPPLIED_LABEL)})
    return result


def parse_market_cap(raw: str) -> float:
    if not raw:
        return 0.0
    cleaned = raw.replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def get_ticker_info(ticker: str) -> dict:
    try:
        return yf.Ticker(ticker).info or {}
    except Exception:
        return {}


def after_hours_snapshot(ticker: str, info: dict | None = None) -> str:
    info = info if info is not None else get_ticker_info(ticker)

    ah_price = info.get("postMarketPrice")
    ah_change_pct = info.get("postMarketChangePercent")
    reg_close = info.get("regularMarketPrice") or info.get("previousClose")

    if ah_price and ah_change_pct is not None:
        return f"AH: ${ah_price:.2f} ({ah_change_pct:+.1f}%) vs close ${reg_close:.2f}"
    return "after-hours data unavailable"


def fetch_earnings_history_finnhub(ticker: str, from_date: str, to_date: str) -> list[dict]:
    """Pull one symbol's earnings calendar rows (one per quarter) from
    Finnhub across a date range, via the same /calendar/earnings endpoint as
    fetch_earnings_calendar_finnhub but filtered to a single symbol so it
    also returns actuals -- epsActual/revenueActual -- once Finnhub has
    them, alongside epsEstimate/revenueEstimate. Used by
    earnings_summary.get_earnings_release(): Finnhub
    populates these actual fields as companies report, with much less lag
    than Yahoo Finance's earnings-dates table (which is what this replaced,
    after Yahoo's data for AST SpaceMobile's 2026-08-10 release still hadn't
    backfilled hours after the release). Returns [] if FINNHUB_API_KEY isn't
    set or the request fails."""
    if not FINNHUB_API_KEY:
        print("FINNHUB_API_KEY not set; skipping Finnhub earnings history fetch.")
        return []
    url = "https://finnhub.io/api/v1/calendar/earnings"
    params = {"from": from_date, "to": to_date, "symbol": ticker, "token": FINNHUB_API_KEY}
    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        return resp.json().get("earningsCalendar") or []
    except Exception as e:
        print(f"Finnhub earnings history fetch failed for {ticker}: {e}")
        return []


def classify_holdings_for_date(date_str: str, tickers: list[str]) -> dict:
    """Returns {ticker: 'bmo' | 'amc' | 'unsupplied'} for tickers that are on
    that date's Finnhub earnings calendar. Tickers not reporting that date
    are simply absent from the returned dict."""
    rows = fetch_earnings_calendar_finnhub(date_str)
    by_symbol = {row.get("symbol"): row for row in rows if row.get("symbol")}

    result = {}
    for ticker in tickers:
        row = by_symbol.get(ticker)
        if not row:
            continue
        time_label = row.get("time")
        if time_label == BMO_LABEL:
            result[ticker] = "bmo"
        elif time_label == AMC_LABEL:
            result[ticker] = "amc"
        else:
            result[ticker] = "unsupplied"
    return result


def arm_earnings_watch(state: dict, ticker: str, hours: int) -> bool:
    """Create the watch record that earnings_watch.py's poll loop acts on.

    This is the single point where a watch is armed, whether it came from you
    texting "earnings for X" or from a holding turning up on the calendar.
    Detection then runs through one code path -- the company's own IR feed or
    page, falling back to news headlines -- rather than the holdings path
    quietly using a weaker source.

    That split was a real bug rather than a tidiness issue. The automatic
    watch used to poll Finnhub's epsActual, which stayed empty all evening on
    2026-08-12 while Cerebras published its results minutes after the close.
    The stocks you actually own were on the least reliable detector.

    Returns True if a NEW watch was created. An existing watch is left alone
    rather than refreshed, because its record carries the list of articles
    already sent -- resetting that would re-send everything.
    """
    key = f"ew_watch::{ticker.upper()}"
    if key in state:
        return False
    now = now_et()
    state[key] = {
        "armed": now.isoformat(),
        "expires": (now + timedelta(hours=hours)).isoformat(),
    }
    return True
