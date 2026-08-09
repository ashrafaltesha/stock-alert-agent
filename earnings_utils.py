"""Shared helpers for pulling earnings-calendar and ticker data, plus the
sleep-until-a-time helper -- used by earnings_watch.py (per-holding reminder
+ release-detection watcher) and market_earnings_watch.py (market-wide
top-cap / most-analyst-attention watcher).
"""

import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import yfinance as yf

from config import EARNINGS_POLL_INTERVAL_SECONDS, EARNINGS_POLL_TIMEOUT_MINUTES

EASTERN = ZoneInfo("America/New_York")

NASDAQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json",
}

# Shared across every earnings_watch.py / market_earnings_watch.py entry
# point: pass --test on the command line to skip all sleeping and use a
# short poll timeout, for a fast manual smoke test via workflow_dispatch.
TEST_MODE = "--test" in sys.argv
POLL_INTERVAL_SECONDS = 5 if TEST_MODE else EARNINGS_POLL_INTERVAL_SECONDS
POLL_TIMEOUT_MINUTES = 1 if TEST_MODE else EARNINGS_POLL_TIMEOUT_MINUTES

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


def sleep_until_et(target_hm: str) -> None:
    """Sleep until today's target local ET time (HH:MM). No-op if that time
    has already passed today, or if running in --test mode."""
    if TEST_MODE:
        print(f"[TEST MODE] skipping sleep-until {target_hm} ET")
        return
    hh, mm = (int(p) for p in target_hm.split(":"))
    now = now_et()
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        print(f"{target_hm} ET has already passed ({now.time()} now) -- continuing immediately.")
        return
    delay = (target - now).total_seconds()
    print(f"Sleeping {delay / 60:.1f} min until {target_hm} ET...")
    time.sleep(delay)


def fetch_earnings_calendar(date_str: str) -> list[dict]:
    """Pull a day's earnings calendar from Nasdaq's public API."""
    url = f"https://api.nasdaq.com/api/calendar/earnings?date={date_str}"
    try:
        resp = requests.get(url, headers=NASDAQ_HEADERS, timeout=20)
        resp.raise_for_status()
        rows = resp.json().get("data", {}).get("rows") or []
        return rows
    except Exception as e:
        print(f"Nasdaq earnings calendar fetch failed for {date_str}: {e}")
        return []


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


def classify_holdings_for_date(date_str: str, tickers: list[str]) -> dict:
    """Returns {ticker: 'bmo' | 'amc' | 'unsupplied'} for tickers that are on
    that date's Nasdaq earnings calendar. Tickers not reporting that date are
    simply absent from the returned dict."""
    rows = fetch_earnings_calendar(date_str)
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
