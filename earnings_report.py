"""
Runs once per weekday after market close (via GitHub Actions cron, ~5:30pm ET).

Sends three sections to Telegram:
  1. Earnings from any of YOUR holdings that reported today.
  2. The top N market-wide companies (by market cap) that reported earnings
     today, with after-hours price action where available.
  3. Additional companies from that day's reporters that are getting the
     most analyst attention (highest number of covering analysts), even if
     they didn't make the market-cap list.

Data sources are free/unofficial (Nasdaq's public earnings calendar + Yahoo
Finance via yfinance). Quality/coverage can vary — treat this as a fast
screener, not a guaranteed-complete feed.
"""

from datetime import datetime, timezone

import requests
import yfinance as yf

from config import (
    TICKERS,
    TOP_N_EARNINGS,
    TOP_N_ANALYST_ATTENTION,
    ANALYST_LOOKUP_POOL_SIZE,
)
from telegram_utils import send_telegram_message

NASDAQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json",
}


def fetch_earnings_calendar(date_str: str) -> list[dict]:
    """Pull today's earnings calendar from Nasdaq's public API."""
    url = f"https://api.nasdaq.com/api/calendar/earnings?date={date_str}"
    try:
        resp = requests.get(url, headers=NASDAQ_HEADERS, timeout=20)
        resp.raise_for_status()
        rows = resp.json().get("data", {}).get("rows") or []
        return rows
    except Exception as e:
        print(f"Nasdaq earnings calendar fetch failed: {e}")
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


def build_holdings_section(date_str: str) -> str:
    lines = ["*Your holdings reporting today:*"]
    found_any = False
    for ticker in TICKERS:
        try:
            cal = yf.Ticker(ticker).get_earnings_dates(limit=4)
        except Exception:
            cal = None
        reported_today = False
        if cal is not None and not cal.empty:
            for idx in cal.index:
                if idx.strftime("%Y-%m-%d") == date_str:
                    reported_today = True
                    break
        if reported_today:
            found_any = True
            lines.append(f"• *{ticker}* — {after_hours_snapshot(ticker)}")
    if not found_any:
        lines.append("None of your holdings reported earnings today.")
    return "\n".join(lines)


def format_row(row: dict, info: dict) -> str:
    symbol = row.get("symbol", "?")
    name = row.get("name", symbol)
    eps = row.get("eps", "n/a")
    eps_forecast = row.get("epsForecast", "n/a")
    timing = row.get("time", "")
    ah = after_hours_snapshot(symbol, info)
    analysts = info.get("numberOfAnalystOpinions")
    analyst_str = f" | {analysts} analysts covering" if analysts else ""
    return (
        f"• *{symbol}* ({name})\n"
        f"  EPS: {eps} vs est. {eps_forecast} | {timing}{analyst_str}\n"
        f"  {ah}"
    )


def build_market_wide_sections(date_str: str) -> tuple[str, str]:
    """Returns (market_cap_section, analyst_attention_section)."""
    rows = fetch_earnings_calendar(date_str)
    if not rows:
        fallback = "No data available (calendar source may be down)."
        return (
            f"*Top market-wide earnings today:*\n{fallback}",
            f"*Most analyst attention today:*\n{fallback}",
        )

    by_market_cap = sorted(rows, key=lambda r: parse_market_cap(r.get("marketCap", "")), reverse=True)
    top_cap = by_market_cap[:TOP_N_EARNINGS]
    top_cap_symbols = {r.get("symbol") for r in top_cap}

    # Only look up analyst coverage for a bounded candidate pool (largest-cap
    # reporters) to keep this fast on busy earnings days.
    candidate_pool = by_market_cap[:ANALYST_LOOKUP_POOL_SIZE]

    info_cache: dict = {}
    for row in candidate_pool:
        symbol = row.get("symbol")
        if symbol:
            info_cache[symbol] = get_ticker_info(symbol)

    # Rank the candidate pool by analyst coverage, excluding anything already
    # shown in the market-cap section so the two lists don't just repeat.
    by_analyst_count = sorted(
        (r for r in candidate_pool if r.get("symbol") not in top_cap_symbols),
        key=lambda r: info_cache.get(r.get("symbol"), {}).get("numberOfAnalystOpinions") or 0,
        reverse=True,
    )
    top_analyst = [r for r in by_analyst_count if info_cache.get(r.get("symbol"), {}).get("numberOfAnalystOpinions")][
        :TOP_N_ANALYST_ATTENTION
    ]

    cap_lines = [f"*Top {len(top_cap)} significant earnings today (by market cap):*"]
    for row in top_cap:
        symbol = row.get("symbol")
        cap_lines.append(format_row(row, info_cache.get(symbol) or get_ticker_info(symbol)))

    analyst_lines = [f"*Most analyst attention today (not already listed above):*"]
    if top_analyst:
        for row in top_analyst:
            analyst_lines.append(format_row(row, info_cache.get(row.get("symbol"), {})))
    else:
        analyst_lines.append("No additional high-coverage names found among today's reporters.")

    return "\n".join(cap_lines), "\n".join(analyst_lines)


def main() -> None:
    date_str = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    header = f"\U0001F4CA *Daily Earnings Report — {date_str}*"

    holdings_section = build_holdings_section(date_str)
    market_cap_section, analyst_section = build_market_wide_sections(date_str)

    # Telegram messages have a ~4096 char limit; send as separate messages to be safe.
    send_telegram_message(f"{header}\n\n{holdings_section}")
    send_telegram_message(market_cap_section)
    send_telegram_message(analyst_section)


if __name__ == "__main__":
    main()
