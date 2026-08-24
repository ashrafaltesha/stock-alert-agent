"""
Runs every 5 minutes during market hours, and hourly the rest of the time
(via two GitHub Actions cron schedules plus an external cron-job.org trigger
that calls this workflow's workflow_dispatch every 5 minutes around the
clock -- see .github/workflows/monitor.yml).

Monitors the union of TICKERS (tickers.json, your held/watched-via-"my list"
symbols) and WATCHLIST (watchlist.json, symbols you want price/news alerts
on without owning them -- see "add TICKER to my watchlist" in
telegram_commands.py) -- both get identical treatment below. For each:
  1. During market hours only: alerts whenever price is
     PRICE_CHANGE_THRESHOLD_PCT or more away from the PRIOR DAY'S CLOSE
     (not a moving checkpoint). E.g. with a 5% threshold: an alert fires
     the first time price is 5%+ above/below yesterday's close, and again
     if it goes on to reach 10%+, 15%+, etc. away from that SAME close --
     each threshold-from-close is a one-time alert per day, in each
     direction (resets each morning to the new previous close).
  2. Anytime (market hours or not): alerts on new, *material* news, pulled
     from both Yahoo Finance's news feed and Google News (which aggregates
     Reuters, CNBC, Bloomberg, MarketWatch and many others).

     What counts as material is decided in news_filter.py, not here. Headlines
     from every ticker and both sources are collected across the whole run and
     screened together at the end -- see process_news_candidates() -- rather
     than judged one at a time as they arrive. That ordering is what allows
     the same wire story appearing on both sources to be collapsed into one
     alert, and lets the run spend a single model call instead of one per
     headline. Anything filtered out is logged and dropped silently.

State (reference price per ticker, already-seen news ids) is kept in
state.json, which this script updates and the workflow commits back to
the repo.
"""

import difflib
import hashlib
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import requests
import yfinance as yf

from config import (
    TICKERS,
    WATCHLIST,
    PRICE_CHANGE_THRESHOLD_PCT,
    NEWS_LOOKBACK_MINUTES,
)
from market_hours import is_market_hours
from telegram_utils import send_telegram_message, escape_markdown
import news_filter
from state_utils import load_state, save_state
from http_utils import get_with_retry, call_with_retry

GOOGLE_NEWS_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def process_news_candidates(candidates, state):
    """Screen a run's worth of articles and send what survives.

    Three stages, cheapest first, because each is more expensive than the
    last and each removes work from the next:

      1. SOURCE     drop aggregators and listicle headlines. No network.
      2. DEDUPE     drop stories already alerted, across both sources.
      3. CLASSIFY   one model call for everything that remains.

    Deduplicating BEFORE classifying matters: Yahoo and Google routinely
    carry the same wire story with slightly different wording, and paying to
    classify the same event twice would be waste as well as a double alert.
    """
    if not candidates:
        return

    kept = []
    for article in candidates:
        if not news_filter.source_allowed(article.get("source"), article["title"]):
            print(f"  [{article['ticker']}] source/shape filtered: "
                  f"{article['title'][:70]}")
            continue
        seen = state.get(f"alerted_titles::{article['ticker']}", [])
        if _is_duplicate_headline(article["title"], seen):
            continue
        # Also dedupe within this batch -- the two sources very often supply
        # the same story in the same run.
        if any(_is_duplicate_headline(article["title"], [k["title"]])
               and k["ticker"] == article["ticker"] for k in kept):
            continue
        kept.append(article)

    if not kept:
        return

    verdicts = news_filter.classify(kept)

    for article, verdict in zip(kept, verdicts):
        send, label = news_filter.should_alert(verdict, article["title"])
        if not send:
            reason = (f"{verdict['impact']}/{verdict['event']}"
                      if verdict else "keyword filter")
            print(f"  [{article['ticker']}] dropped ({reason}): "
                  f"{article['title'][:70]}")
            continue

        msg = f"\U0001F4F0 *{article['ticker']}*"
        if label:
            msg += f" — {escape_markdown(label)}"
        msg += f"\n{escape_markdown(article['title'])}"
        if article.get("source"):
            msg += f"\n_{escape_markdown(article['source'])}_"
        if article.get("link"):
            msg += f"\n{article['link']}"
        send_telegram_message(msg)
        _record_headline(article["ticker"], article["title"], state)


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
        info = call_with_retry(
            lambda: yf.Ticker(ticker).fast_info, label=f"{ticker} price"
        )
        last_price = info["last_price"]
        prev_close = info["previous_close"]
    except Exception as e:
        print(f"[{ticker}] price fetch failed after retries: {e}")
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


_NEWS_ID_SCHEME = "sha1-16"


def _article_key(raw) -> str:
    """Short, stable dedup key for a news article.

    Google News guids run roughly 270 characters each, and with 100 kept
    per ticker per source they came to dominate state.json (which had
    grown past 190KB). A truncated hash dedups exactly as well at a small
    fraction of the size.
    """
    return hashlib.sha1(str(raw).encode("utf-8", "replace")).hexdigest()[:16]


def _migrate_news_ids(state: dict) -> None:
    """One-time conversion of previously stored raw article ids to hashes.

    Without this, every already-seen article would look new on the first
    run after deploy and re-alert. Runs once and records the scheme in
    state so it never repeats.
    """
    if state.get("news_id_scheme") == _NEWS_ID_SCHEME:
        return
    converted = 0
    for key in list(state.keys()):
        if not (key.startswith("seen_news::") or key.startswith("seen_news_google::")):
            continue
        new_ids = []
        for value in state.get(key) or []:
            text = str(value)
            # Entries already in the new form are 16 lowercase hex chars.
            if len(text) == 16 and all(c in "0123456789abcdef" for c in text):
                new_ids.append(text)
            else:
                new_ids.append(_article_key(text))
                converted += 1
        state[key] = new_ids
    state["news_id_scheme"] = _NEWS_ID_SCHEME
    print(f"Migrated {converted} stored news ids to {_NEWS_ID_SCHEME} hashes.")


def _company_name(ticker: str, state: dict) -> str:
    """Resolve and cache a ticker's company name.

    Cached in state indefinitely: names change very rarely and the
    yfinance .info call behind this is slow, so we must not repeat it on
    every one-minute run. Delete the cached key to force a re-resolve.
    """
    key = f"company_name::{ticker}"
    if key in state:
        return state[key] or ""
    name = ""
    try:
        info = call_with_retry(lambda: yf.Ticker(ticker).info, label=f"{ticker} info") or {}
        name = (info.get("shortName") or info.get("longName") or "").strip()
    except Exception as e:
        print(f"[{ticker}] company-name lookup failed: {e}")
    # Cache even an empty result so a persistently failing lookup doesn't
    # re-run every minute.
    state[key] = name
    if name:
        print(f"[{ticker}] resolved company name: {name}")
    return name


def _news_query(ticker: str, state: dict) -> str:
    """Build the Google News search query for a ticker.

    Searching "TICKER stock" is badly ambiguous when the ticker is an
    ordinary English word -- FOUR, WOLF, APP -- and pulled in a lot of
    unrelated articles. Where the company name resolves we search that as
    a quoted phrase instead, which is far more precise. Falls back to the
    old form when the name isn't available.
    """
    name = _company_name(ticker, state)
    if name:
        return f'"{name}"'
    return f"{ticker} stock"


def _is_duplicate_headline(title: str, recent_titles: list, threshold: float = 0.82) -> bool:
    """True if `title` closely matches something already alerted for this
    ticker from either news source -- Yahoo's own feed and Google News RSS
    often carry the same wire story with slightly different wording, so a
    per-source id dedup alone lets the same story through twice."""
    t = title.lower().strip()
    for prev in recent_titles:
        if difflib.SequenceMatcher(None, t, prev).ratio() >= threshold:
            return True
    return False


def _record_headline(ticker: str, title: str, state: dict) -> None:
    key = f"alerted_titles::{ticker}"
    titles = state.get(key, [])
    titles.append(title.lower().strip())
    state[key] = titles[-40:]


def check_yahoo_news(ticker: str, state: dict, candidates: list) -> None:
    key = f"seen_news::{ticker}"
    seen_ids = set(state.get(key, []))
    now = datetime.now(timezone.utc)

    try:
        articles = call_with_retry(
            lambda: yf.Ticker(ticker).news, label=f"{ticker} yahoo-news"
        ) or []
    except Exception as e:
        print(f"[{ticker}] news fetch failed after retries: {e}")
        return

    new_ids = list(seen_ids)
    for art in articles:
        content = art.get("content", art)  # yfinance news schema has varied over versions
        article_id = _article_key(content.get("id") or art.get("uuid") or content.get("title"))
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
        if age_minutes <= NEWS_LOOKBACK_MINUTES:
            link = (
                content.get("canonicalUrl", {}).get("url")
                or content.get("clickThroughUrl", {}).get("url")
                or ""
            )
            publisher = content.get("provider", {}).get("displayName", "") if isinstance(
                content.get("provider"), dict
            ) else content.get("publisher", "")
            candidates.append({"ticker": ticker, "title": title,
                               "source": publisher, "link": link})

    # Cap stored ids so state.json doesn't grow forever.
    state[key] = new_ids[-100:]


def check_google_news(ticker: str, state: dict, candidates: list) -> None:
    """Pull recent headlines from Google News RSS, which aggregates across
    outlets (Reuters, CNBC, Bloomberg, MarketWatch, Benzinga, Seeking Alpha,
    Motley Fool, etc.) rather than relying on Yahoo Finance alone."""
    key = f"seen_news_google::{ticker}"
    seen_ids = set(state.get(key, []))
    now = datetime.now(timezone.utc)

    query = quote(_news_query(ticker, state))
    url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"

    resp = get_with_retry(url, headers=GOOGLE_NEWS_HEADERS, timeout=15,
                          label=f"{ticker} google-news")
    if resp is None:
        print(f"[{ticker}] Google News unavailable this run -- skipping.")
        return
    try:
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        items = root.findall("./channel/item")
    except Exception as e:
        print(f"[{ticker}] Google News parse failed: {e}")
        return
    new_ids = list(seen_ids)
    for item in items:
        guid = item.findtext("guid") or item.findtext("link") or item.findtext("title")
        article_id = _article_key(guid)
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
        if age_minutes <= NEWS_LOOKBACK_MINUTES:
            link = item.findtext("link") or ""
            source_el = item.find("source")
            publisher = source_el.text if source_el is not None else "Google News"
            candidates.append({"ticker": ticker, "title": title,
                               "source": publisher, "link": link})

    state[key] = new_ids[-100:]


def main() -> None:
    force = "--force" in sys.argv  # allow manual testing of price checks outside market hours
    market_open = is_market_hours()

    if market_open or force:
        print("Market hours: checking prices + material news.")
    else:
        print("Outside market hours: checking material news only.")

    state = load_state()
    _migrate_news_ids(state)
    # Dedup in case a symbol is on both lists (e.g. you added it to your
    # watchlist before buying it) -- avoids fetching/checking it twice.
    monitored = sorted(set(TICKERS) | set(WATCHLIST))

    # Collected across every ticker and BOTH sources, then screened in one
    # batch. Sending from inside the fetch loop -- as this used to -- meant
    # the same wire story could be alerted twice, once per source, and made
    # per-article classification the only option.
    candidates = []

    for ticker in monitored:
        if market_open or force:
            check_price_moves(ticker, state)
        # News checks always run -- 5-min cadence during market hours,
        # hourly the rest of the time, per the two cron schedules.
        check_yahoo_news(ticker, state, candidates)
        check_google_news(ticker, state, candidates)

    print(f"{len(candidates)} candidate article(s) this run.")
    process_news_candidates(candidates, state)
    save_state(state)

    # This workflow is driven by an external cron every five minutes and is
    # therefore punctual, which the listener's own hourly schedule is not.
    # Cheapest reliable place to notice the bot has stopped answering.
    try:
        from workflow_trigger import ensure_listener_running
        ensure_listener_running()
    except Exception as e:
        print(f"Listener check failed (non-fatal): {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
