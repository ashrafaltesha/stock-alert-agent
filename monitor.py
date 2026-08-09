"""
Runs every 15 minutes during market hours (via GitHub Actions cron).

For each ticker you own:
  1. Alerts if the price has moved >= PRICE_CHANGE_THRESHOLD_PCT from the
     previous close (once per direction per day, so it won't spam you).
  2. Alerts on any new news headline seen since the last run, pulled from
     both Yahoo Finance's news feed and Google News (which aggregates
     Reuters, CNBC, Bloomberg, MarketWatch, Benzinga, Seeking Alpha, etc.).

State (what's already been alerted today) is kept in state.json, which this
script updates and the workflow commits back to the repo.
"""

import json
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import requests
import yfinance as yf

from config import (
    TICKERS,
    PRICE_CHANGE_THRESHOLD_PCT,
    NEWS_LOOKBACK_MINUTES,
    STATE_FILE,
)
from market_hours import is_market_hours
from telegram_utils import send_telegram_message

GOOGLE_NEWS_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def check_price_moves(ticker: str, state: dict) -> None:
    key = f"price_alert::{ticker}"
    today = today_str()
    already = state.get(key, {})
    if already.get("date") == today and already.get("alerted"):
        return  # already alerted for this ticker today

    try:
        info = yf.Ticker(ticker).fast_info
        last_price = info["last_price"]
        prev_close = info["previous_close"]
    except Exception as e:
        print(f"[{ticker}] price fetch failed: {e}")
        return

    if not prev_close:
        return

    pct_change = (last_price - prev_close) / prev_close * 100

    if abs(pct_change) >= PRICE_CHANGE_THRESHOLD_PCT:
        emoji = "\U0001F4C8" if pct_change > 0 else "\U0001F4C9"
        msg = (
            f"{emoji} *{ticker}* moved {pct_change:+.1f}% today\n"
            f"Last: ${last_price:.2f}  |  Prev close: ${prev_close:.2f}"
        )
        send_telegram_message(msg)
        state[key] = {"date": today, "alerted": True, "pct_change": round(pct_change, 2)}


def check_yahoo_news(ticker: str, state: dict) -> None:
    key = f"seen_news::{ticker}"
    seen_ids = set(state.get(key, []))
    now = datetime.now(timezone.utc)

    try:
        articles = yf.Ticker(ticker).news or []
    except Exception as e:
        print(f"[{ticker}] news fetch failed: {e}")
        return

    new_ids = list(seen_ids)
    for art in articles:
        content = art.get("content", art)  # yfinance news schema has varied over versions
        article_id = str(content.get("id") or art.get("uuid") or content.get("title"))
        if article_id in seen_ids:
            continue

        pub_time = None
        for time_field in ("pubDate", "providerPublishTime", "displayTime"):
            if content.get(time_field):
                pub_time = content[time_field]
                break

        try:
            if isinstance(pub_time, (int, float)):
                published = datetime.fromtimestamp(pub_time, tz=timezone.utc)
            elif isinstance(pub_time, str):
                published = datetime.fromisoformat(pub_time.replace("Z", "+00:00"))
            else:
                published = now  # unknown format, treat as fresh
        except Exception:
            published = now

        age_minutes = (now - published).total_seconds() / 60

        # Always remember we've seen it, but only alert if it's fresh.
        new_ids.append(article_id)
        if age_minutes <= NEWS_LOOKBACK_MINUTES:
            title = content.get("title") or "New article"
            link = (
                content.get("canonicalUrl", {}).get("url")
                or content.get("clickThroughUrl", {}).get("url")
                or ""
            )
            publisher = content.get("provider", {}).get("displayName", "") if isinstance(
                content.get("provider"), dict
            ) else content.get("publisher", "")
            msg = f"\U0001F4F0 *{ticker} news*: {title}"
            if publisher:
                msg += f"\n_{publisher}_"
            if link:
                msg += f"\n{link}"
            send_telegram_message(msg)

    # Cap stored ids so state.json doesn't grow forever.
    state[key] = new_ids[-100:]


def check_google_news(ticker: str, state: dict) -> None:
    """Pull recent headlines from Google News RSS, which aggregates across
    outlets (Reuters, CNBC, Bloomberg, MarketWatch, Benzinga, Seeking Alpha,
    Motley Fool, etc.) rather than relying on Yahoo Finance alone."""
    key = f"seen_news_google::{ticker}"
    seen_ids = set(state.get(key, []))
    now = datetime.now(timezone.utc)

    query = quote(f"{ticker} stock")
    url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"

    try:
        resp = requests.get(url, headers=GOOGLE_NEWS_HEADERS, timeout=15)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        items = root.findall("./channel/item")
    except Exception as e:
        print(f"[{ticker}] Google News fetch failed: {e}")
        return

    new_ids = list(seen_ids)
    for item in items:
        guid = item.findtext("guid") or item.findtext("link") or item.findtext("title")
        article_id = str(guid)
        if article_id in seen_ids:
            continue

        pub_date_raw = item.findtext("pubDate")
        try:
            published = parsedate_to_datetime(pub_date_raw) if pub_date_raw else now
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
        except Exception:
            published = now

        age_minutes = (now - published).total_seconds() / 60

        new_ids.append(article_id)
        if age_minutes <= NEWS_LOOKBACK_MINUTES:
            title = item.findtext("title") or "New article"
            link = item.findtext("link") or ""
            source_el = item.find("source")
            publisher = source_el.text if source_el is not None else "Google News"
            msg = f"\U0001F4F0 *{ticker} news*: {title}"
            if publisher:
                msg += f"\n_{publisher}_"
            if link:
                msg += f"\n{link}"
            send_telegram_message(msg)

    state[key] = new_ids[-100:]


def main() -> None:
    force = "--force" in sys.argv  # allow manual testing outside market hours
    if not force and not is_market_hours():
        print("Outside market hours, skipping.")
        return

    state = load_state()
    for ticker in TICKERS:
        check_price_moves(ticker, state)
        check_yahoo_news(ticker, state)
        check_google_news(ticker, state)
    save_state(state)


if __name__ == "__main__":
    main()
