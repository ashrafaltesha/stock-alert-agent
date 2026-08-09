"""
Runs every 5 minutes during market hours, and hourly the rest of the time
(via two GitHub Actions cron schedules -- see .github/workflows/monitor.yml).

For each ticker you own:
  1. During market hours only: alerts on every sequential
     PRICE_CHANGE_THRESHOLD_PCT move, in either direction, starting from
     the previous close. E.g. with a 5% threshold: first alert fires at
     +5% from close; if it then moves another 5% up OR down from THAT
     point, another alert fires, and so on throughout the day (resets
     each morning to the new previous close).
  2. Anytime (market hours or not): alerts on new, *material* news --
     analyst upgrades/downgrades, M&A/partnership/collaboration
     announcements, delivery/production numbers, and similar catalysts
     (see MATERIAL_NEWS_KEYWORDS in config.py) -- pulled from both Yahoo
     Finance's news feed and Google News (which aggregates Reuters, CNBC,
     Bloomberg, MarketWatch, Benzinga, Seeking Alpha, etc.). Routine/non-
     material headlines are tracked for dedup but not sent.

State (reference price per ticker, already-seen news ids) is kept in
state.json, which this script updates and the workflow commits back to
the repo.
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
    MATERIAL_NEWS_KEYWORDS,
    STATE_FILE,
)
from market_hours import is_market_hours
from telegram_utils import send_telegram_message

GOOGLE_NEWS_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def is_material(title: str) -> bool:
    """Keyword-based material-news filter. Simple substring matching --
    not true NLP classification, so it can occasionally miss unusually
    worded stories or match a loosely related one. Tune the keyword list
    in config.py if it's too noisy or too quiet."""
    title_lower = title.lower()
    return any(keyword in title_lower for keyword in MATERIAL_NEWS_KEYWORDS)


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
    """Sequential-threshold alerting: fires every time price moves
    PRICE_CHANGE_THRESHOLD_PCT from the last alerted reference point (not
    just from the day's previous close), in either direction. Resets each
    trading day. If a single check catches a move spanning multiple
    thresholds (e.g. a 12% gap-up), it sends one alert per threshold
    crossed, walking the reference price forward in threshold-sized steps.
    """
    key = f"price_ref::{ticker}"
    today = today_str()

    try:
        info = yf.Ticker(ticker).fast_info
        last_price = info["last_price"]
        prev_close = info["previous_close"]
    except Exception as e:
        print(f"[{ticker}] price fetch failed: {e}")
        return

    if not prev_close:
        return

    saved = state.get(key, {})
    if saved.get("date") == today:
        reference_price = saved.get("reference_price", prev_close)
        moves_today = saved.get("moves_today", 0)
    else:
        # New trading day: reset the reference point to today's previous close.
        reference_price = prev_close
        moves_today = 0

    threshold = PRICE_CHANGE_THRESHOLD_PCT / 100

    # Walk the reference price toward last_price in threshold-sized steps,
    # sending one alert per step crossed (usually 0 or 1 per run, but can
    # be more than one if the price gapped hard between checks).
    while True:
        pct_from_ref = (last_price - reference_price) / reference_price * 100
        if abs(pct_from_ref) < PRICE_CHANGE_THRESHOLD_PCT:
            break

        direction_up = pct_from_ref > 0
        new_reference = reference_price * (1 + threshold if direction_up else 1 - threshold)
        moves_today += 1
        pct_from_close = (new_reference - prev_close) / prev_close * 100

        emoji = "\U0001F4C8" if direction_up else "\U0001F4C9"
        msg = (
            f"{emoji} *{ticker}* move #{moves_today} today: "
            f"{'up' if direction_up else 'down'} {PRICE_CHANGE_THRESHOLD_PCT:.1f}% "
            f"from its last checkpoint\n"
            f"Now: ${last_price:.2f}  |  {pct_from_close:+.1f}% vs prev close (${prev_close:.2f})"
        )
        send_telegram_message(msg)
        reference_price = new_reference

    state[key] = {
        "date": today,
        "reference_price": reference_price,
        "moves_today": moves_today,
    }


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

        # Always remember we've seen it (so it's never re-evaluated), but
        # only alert if it's fresh AND passes the material-news filter.
        new_ids.append(article_id)
        title = content.get("title") or "New article"
        if age_minutes <= NEWS_LOOKBACK_MINUTES and is_material(title):
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
        title = item.findtext("title") or "New article"
        if age_minutes <= NEWS_LOOKBACK_MINUTES and is_material(title):
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
    force = "--force" in sys.argv  # allow manual testing of price checks outside market hours
    market_open = is_market_hours()

    if market_open or force:
        print("Market hours: checking prices + material news.")
    else:
        print("Outside market hours: checking material news only.")

    state = load_state()
    for ticker in TICKERS:
        if market_open or force:
            check_price_moves(ticker, state)
        # News checks always run -- 5-min cadence during market hours,
        # hourly the rest of the time, per the two cron schedules.
        check_yahoo_news(ticker, state)
        check_google_news(ticker, state)
    save_state(state)


if __name__ == "__main__":
    main()
