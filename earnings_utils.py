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
  symbol across a date range instead of one date across all symbols. It also
  surfaces epsActual/revenueActual once Finnhub has them, but nothing relies
  on those any more: detection is SEC filings. Its one live caller is
  fetch_consensus() below, which reads epsEstimate -- a number that is set
  BEFORE a company reports and therefore does not lag, unlike epsActual,
  which stayed empty all evening while Cerebras published minutes after the
  close.
"""

import sys
from datetime import datetime, timedelta

import requests
import yfinance as yf

from config import FINNHUB_API_KEY
# Re-exported, not redefined. These are stdlib-only and live in timeutil so
# the earnings watcher can import them without dragging requests and yfinance
# into a job that installs neither.
from timeutil import EASTERN, arm_earnings_watch, date_str_et, now_et  # noqa: F401

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
    fetch_consensus() below. Returns [] if FINNHUB_API_KEY isn't set or the
    request fails."""
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


def _label_to_category(time_label):
    if time_label == BMO_LABEL:
        return "bmo"
    if time_label == AMC_LABEL:
        return "amc"
    return "unsupplied"


def classify_holdings_for_date(date_str: str, tickers: list[str]) -> dict:
    """Returns {ticker: 'bmo' | 'amc' | 'unsupplied'} for tickers reporting
    that date, per EITHER Finnhub or Nasdaq. Absent means neither lists it.

    Two calendars, unioned, because arming is the single point of failure in
    the whole earnings system: no watch armed means the SEC detection never
    runs, however good it is. A calendar that misses a company silently
    disables everything downstream for that company.

    And free calendars do miss. Wall Street Horizon sells confirmed-versus-
    estimated earnings dates to institutions for real money precisely because
    this is a hard data problem -- the failure modes cluster on recent IPOs
    and foreign issuers, which describes Cerebras, Genius Sports and XPeng.

    Unioning is the right operator rather than intersecting: a wasted watch
    costs a few hundred HTTP requests and expires quietly after 24 hours,
    while a missed one costs the alert entirely. The two are not comparable,
    so any single source saying "reporting" is enough.

    Timing (bmo/amc) no longer gates anything -- polling runs from 05:05 to
    20:00 ET regardless -- so a disagreement between sources only affects the
    wording of the heads-up message. The more specific answer wins.
    """
    sources = []
    try:
        sources.append(("finnhub", fetch_earnings_calendar_finnhub(date_str)))
    except Exception as e:
        print(f"Finnhub calendar failed for {date_str}: {type(e).__name__}: {e}")
    try:
        sources.append(("nasdaq", fetch_earnings_calendar(date_str)))
    except Exception as e:
        print(f"Nasdaq calendar failed for {date_str}: {type(e).__name__}: {e}")

    wanted = {t.upper() for t in tickers}
    result = {}
    found_by = {}

    for name, rows in sources:
        for row in rows or []:
            symbol = str(row.get("symbol") or "").upper()
            if symbol not in wanted:
                continue
            category = _label_to_category(row.get("time"))
            found_by.setdefault(symbol, []).append(name)
            # A source that actually knows the timing beats one that says
            # "not supplied"; otherwise first writer wins.
            if symbol not in result or (result[symbol] == "unsupplied"
                                        and category != "unsupplied"):
                result[symbol] = category

    for symbol, names in sorted(found_by.items()):
        print(f"[{symbol}] {date_str}: listed by {', '.join(sorted(set(names)))} "
              f"-> {result[symbol]}")

    return result


def fetch_consensus(ticker: str, date_str: str) -> dict:
    """Analyst EPS estimate for a ticker's report on a given date.

    Fetched when the watch is ARMED, not when the filing lands. That ordering
    is the point: Finnhub's epsActual lags badly -- it stayed empty all
    evening while Cerebras published minutes after the close, which is what
    started the move to SEC filings. But epsEstimate is set BEFORE the company
    reports and does not lag at all, so it is safe to rely on.

    Storing it up front also means the beat/miss line costs nothing at alert
    time, when latency actually matters.

    Returns {} when unavailable; the caller simply omits the comparison.
    """
    rows = fetch_earnings_history_finnhub(ticker, date_str, date_str)
    for row in rows or []:
        if str(row.get("symbol", "")).upper() != ticker.upper():
            continue
        estimate = row.get("epsEstimate")
        if estimate is None:
            continue
        return {"eps_estimate": estimate, "date": date_str}
    return {}
