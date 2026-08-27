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
import health
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
        health.record_alert(state, "news")
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
        health.record_alert(state, "price")

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
            # Entries may be a bare id (old) or [id, minute_stamp] (current).
            if isinstance(value, (list, tuple)) and len(value) == 2:
                new_ids.append(list(value))
                continue
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


# How long a seen-article id is worth remembering.
#
# An article is only ever alerted while it is younger than
# NEWS_LOOKBACK_MINUTES, so an id older than that cannot produce an alert
# however many times it is re-read -- the age check rejects it first.
# Remembering the last 100 ids per ticker per source was therefore storing
# roughly ten times more than correctness requires, and seen_news was 85% of
# state.json: 26 lists of 100 ids each, rewritten and committed ~1,380 times
# a day, which is what made the repository's pack 90 MiB.
#
# Three times the lookback, not one, as margin against republished pubDates
# and clock skew. Fuzzy title dedupe (alerted_titles) is the real defence
# against a genuinely recycled story, and it is untouched.
ID_RETENTION_MINUTES = NEWS_LOOKBACK_MINUTES * 3

# How many undated legacy ids to carry across the one-time changeover.
# Comfortably more than a lookback window's worth of articles.
LEGACY_CARRY = 25


def _now_minutes() -> int:
    return int(datetime.now(timezone.utc).timestamp() // 60)


def _load_seen(state: dict, key: str):
    """Returns {article_id: minute_stamp}, dropping anything too old to matter.

    Accepts the old plain-list form and stamps it as of now, so ids carried
    over from before this change age out over the next few hours instead of
    vanishing at once. Forgetting them in a single step would let anything
    still inside the lookback window alert a second time.
    """
    raw = state.get(key) or []
    now = _now_minutes()

    # Carrying over the whole legacy list would stamp 100 undated ids as
    # "now" and hold them for the full retention window, which measured out
    # at temporarily DOUBLING state.json before it shrank -- the opposite of
    # the point. Only the newest matter: the old writer appended in order, so
    # the tail is the most recent, and only ids inside the lookback window
    # could re-alert anyway.
    legacy = [e for e in raw if not (isinstance(e, (list, tuple)) and len(e) == 2)]
    if len(legacy) > LEGACY_CARRY:
        drop = set(map(str, legacy[:-LEGACY_CARRY]))
        raw = [e for e in raw
               if (isinstance(e, (list, tuple)) and len(e) == 2) or str(e) not in drop]

    seen = {}
    for entry in raw:
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            article_id, stamp = entry[0], int(entry[1])
        else:
            article_id, stamp = str(entry), now
        if now - stamp <= ID_RETENTION_MINUTES:
            seen[article_id] = stamp
    return seen


def _save_seen(state: dict, key: str, seen: dict) -> None:
    """Prune on the way out as well as on the way in.

    Pruning only on load looked sufficient -- the working set is loaded,
    added to, and saved -- but it left the bound depending on the caller
    never handing this a large dict. A retention rule that is only enforced
    on one side of the round trip is not a bound, and the test that caught
    this wrote 400 entries straight through.
    """
    now = _now_minutes()
    fresh = {a: t for a, t in seen.items() if now - t <= ID_RETENTION_MINUTES}
    # Sorted for a stable diff: an unordered rewrite would show every line as
    # changed on every commit, which is its own kind of churn.
    state[key] = [[a, t] for a, t in sorted(fresh.items(), key=lambda kv: (kv[1], kv[0]))]


def _record_headline(ticker: str, title: str, state: dict) -> None:
    key = f"alerted_titles::{ticker}"
    titles = state.get(key, [])
    titles.append(title.lower().strip())
    state[key] = titles[-40:]


def check_yahoo_news(ticker: str, state: dict, candidates: list) -> None:
    key = f"seen_news::{ticker}"
    seen = _load_seen(state, key)
    seen_ids = set(seen)
    now = datetime.now(timezone.utc)

    try:
        articles = call_with_retry(
            lambda: yf.Ticker(ticker).news, label=f"{ticker} yahoo-news"
        ) or []
    except Exception as e:
        print(f"[{ticker}] news fetch failed after retries: {e}")
        return

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

        # Remember only what could still alert.
        #
        # This used to remember EVERY item in the feed. Both feeds return
        # around a hundred items per query regardless of age, so state.json
        # was carrying the whole feed: measured live, seen_news_google::RDDT
        # held 144 ids all stamped within four minutes, and the file grew to
        # 94 KB. The old [-100:] cap hid this by truncating; switching to
        # time-based retention removed the cap and exposed it.
        #
        # An article outside the lookback window cannot alert, now or on any
        # later run -- the age check below rejects it first -- so recording
        # it buys nothing. Forgetting it costs one re-parse of data already
        # in hand.
        title = content.get("title") or "New article"
        if age_minutes <= NEWS_LOOKBACK_MINUTES:
            seen[article_id] = _now_minutes()
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

    _save_seen(state, key, seen)


def check_google_news(ticker: str, state: dict, candidates: list) -> None:
    """Pull recent headlines from Google News RSS, which aggregates across
    outlets (Reuters, CNBC, Bloomberg, MarketWatch, Benzinga, Seeking Alpha,
    Motley Fool, etc.) rather than relying on Yahoo Finance alone."""
    key = f"seen_news_google::{ticker}"
    seen = _load_seen(state, key)
    seen_ids = set(seen)
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

        # Same rule as the Yahoo collector: only remember what could still
        # alert. Google News is the worse offender -- it returns ~100 items
        # per query however old they are.
        title = item.findtext("title") or "New article"
        if age_minutes <= NEWS_LOOKBACK_MINUTES:
            seen[article_id] = _now_minutes()
            link = item.findtext("link") or ""
            source_el = item.find("source")
            publisher = source_el.text if source_el is not None else "Google News"
            candidates.append({"ticker": ticker, "title": title,
                               "source": publisher, "link": link})

    _save_seen(state, key, seen)


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
    # Two watchdogs, on the workflow that is actually punctual. This one runs
    # every minute via the external cron; the schedules these protect do not.
    try:
        from workflow_trigger import ensure_listener_running, ensure_watcher_running
        ensure_listener_running()
        ensure_watcher_running(state)
    except Exception as e:
        print(f"Watchdog check failed (non-fatal): {type(e).__name__}: {e}")

    # Last thing in the run, so a ping means the whole run completed. Placing
    # it earlier would report health for a run that later died.
    import heartbeat
    heartbeat.ping(heartbeat.MONITOR)

    # Recorded last, so a timestamp means the whole run finished.
    health.record(state, "monitor")
    save_state(state)


if __name__ == "__main__":
    main()
