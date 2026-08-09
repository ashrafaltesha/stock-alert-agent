"""
Runs every 5 minutes during market hours, and hourly the rest of the time
(via two GitHub Actions cron schedules -- see .github/workflows/monitor.yml).

For each ticker you own:
  1. During market hours only: alerts whenever price is
     PRICE_CHANGE_THRESHOLD_PCT or more away from the PRIOR DAY'S CLOSE
     (not a moving checkpoint). E.g. with a 5% threshold: an alert fires
     the first time price is 5%+ above/below yesterday's close, and again
     if it goes on to reach 10%+, 15%+, etc. away from that SAME close --
     each threshold-from-close is a one-time alert per day, in each
     direction (resets each morning to the new previous close).
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
)
from market_hours import is_market_hours
from telegram_utils import send_telegram_message
from state_utils import load_state, save_state

GOOGLE_NEWS_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def is_material(title: str) -> bool:
    """Keyword-based material-news filter. Simple substring matching --
    not true NLP classification, so it can occasionally miss unusually
    worded stories or match a loosely related one. Tune the keyword list
    in config.py if it's too noisy or too quiet."""
    title_lower = title.lower()
    return any(keyword in title_lower for keyword in MATERIAL_NEWS_KEYWORDS)


def today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def check_price_moves(ticker: str, state: dict) -> None:
    """Fixed-anchor threshold alerting: fires whenever price is
    PRICE_CHANGE_THRESHOLD_PCT or more away from the PRIOR DAY'S CLOSE,
    in either direction. Unlike a moving-checkpoint scheme, the anchor
    never changes during the day -- only the day's previous close does,
    each morning. Each threshold step away from that close (5%, 10%,
    15%, ...) alerts at most once per day per direction; if a single
    check catches a move spanning multiple thresholds (e.g. a 12%
    gap-up), it sends one alert per threshold crossed.
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
        max_step_up = saved.get("max_step_up", 0)
        max_step_down = saved.get("max_step_down", 0)
    else:
        # New trading day: reset both directions' step counters.
        max_step_up = 0
        max_step_down = 0

    pct_from_close = (last_price - prev_close) / prev_close * 100
    direction_up = pct_from_close >= 0
    target_step = int(abs(pct_from_close) // PRICE_CHANGE_THRESHOLD_PCT)
    max_step = max_step_up if direction_up else max_step_down

    # Send one alert per threshold step newly crossed (usually 0 or 1 per
    # run, but can be more than one if the price gapped hard between checks).
    while max_step < target_step:
        max_step += 1
        step_pct = max_step * PRICE_CHANGE_THRESHOLD_PCT
        emoji = "\U0001F4C8" if direction_up else "\U0001F4C9"
        msg = (
            f"{emoji} *{ticker}* now {step_pct:.0f}%+ {'above' if direction_up else 'below'} "
            f"yesterday's close\n"
            f"Now: ${last_price:.2f}  |  {pct_from_close:+.1f}% vs prev close (${prev_close:.2f})"
        )
        send_telegram_message(msg)

    if direction_up:
        max_step_up = max_step
    else:
        max_step_down = max_step

    state[key] = {
        "date": today,
        "max_step_up": max_step_up,
        "max_step_down": max_step_down,
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
