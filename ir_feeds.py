"""Earnings-release detection from company investor-relations RSS feeds.

Why IR feeds and nothing else
-----------------------------
Reachability was measured from the GitHub Actions runner rather than assumed,
after the EDGAR attempt failed for exactly that reason. Results:

    investors.cerebras.ai/rss/news-releases.xml   200  (contained the release)
    IR HTML pages                                 timeout
    GlobeNewswire (RSS + search)                  timeout
    Business Wire                                 timeout
    sec.gov / efts.sec.gov                        403
    news.google.com/rss                           200

So the company's own RSS feed is the one authoritative source that answers
from CI. Wire services and SEC block or throttle datacenter IP ranges; the
IR *pages* hang while the *feeds* respond.

A consequence worth knowing: because the linked release page can't be
fetched, the summary is built from the feed entry itself (title plus
description). That is thinner than parsing a full press release, but it
arrives within a minute or two of publication, which is the point.

Two sources, not one
--------------------
Finding IR feed URLs stalled at 2 of 9 tickers -- the rest 404 on every
guessed path or fail DNS outright. Rather than leave seven tickers with no
detection at all, Google News backs them up: monitor.py already polls it
every minute for all nine, so it costs nothing new and is proven from CI.

Paid alternatives were measured rather than assumed and all rejected:
FMP's press-release feed returns HTTP 402 on the free tier, its 8-K feed
refreshes only hourly, and SEC-API.io would have been $49/mo.

The two sources are not interchangeable. An IR feed carries the company's
own words, often with figures. Google News carries somebody's *article
about* the release, so its headline vocabulary is different and its body is
near-useless -- which is why there are two classifiers below, not one. Alerts
sourced from news say so explicitly.
"""

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from http_utils import get_with_retry

FEED_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; stock-alert-agent/1.0)",
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
}

# Verified working from the runner. Populated from probe_sources.py results --
# do not add a URL here without confirming it returns 200 *from CI*, since a
# feed that loads fine from a laptop may still be blocked on the runner.
FEED_URLS = {
    "CBRS": "https://investors.cerebras.ai/rss/news-releases.xml",
    "XPEV": "https://ir.xiaopeng.com/rss/news-releases.xml",
}
# The other seven (GENI, EVH, QURE, UAVS, WOLF, FOUR, APP) have no feed at any
# path tried: 404 where the host answers, DNS failure where it doesn't. They
# fall through to Google News. Adding one here later is a pure upgrade -- it
# takes precedence automatically and the alert gets richer.

# Title patterns that mark an entry as the quarterly results release, rather
# than any other company announcement. Deliberately tighter than the general
# material-news keyword list: this decides whether to declare "earnings are
# out", so a partnership or product headline must not qualify.
_TITLE_PATTERNS = (
    r"\bfirst[- ]quarter\b",
    r"\bsecond[- ]quarter\b",
    r"\bthird[- ]quarter\b",
    r"\bfourth[- ]quarter\b",
    r"\bq[1-4]\b",
    r"\bfull[- ]year\b",
    r"\bfiscal\s+(?:year|20\d\d)\b",
    r"\bannual results\b",
)

# A results word in the TITLE is sufficient but not necessary. Companies
# frequently lead with the story instead: Cerebras titled its Q2 2026 release
# "Fast Inference Cloud Business Nearly Quadruples in Second Quarter 2026",
# which contains no results word at all. Requiring one here would have missed
# the very release this module exists to catch.
_RESULT_WORDS = (
    r"\bresults\b",
    r"\bearnings\b",
    r"\breports\b",
    r"\breported\b",
)

# ...so when the title alone is ambiguous, fall back to the body. An earnings
# release always quotes financial statements; a partnership or product
# announcement carrying a quarter reference does not.
_FINANCIAL_MARKERS = (
    r"\brevenue\b",
    r"\bgaap\b",
    r"\bper share\b",
    r"\beps\b",
    r"\bnet (?:income|loss)\b",
    r"\bgross margin\b",
    r"\boperating margin\b",
    r"\bguidance\b",
    r"\boutlook\b",
)

# Scheduling / logistics announcements that mention results but are not one.
_EXCLUDE = (
    r"\bwill (?:release|report|announce)\b",
    r"\bto (?:release|report|announce)\b",
    r"\bsets? date\b",
    r"\bconference call\b",
    r"\bwebcast\b",
    r"\binvites?\b",
    r"\bschedule[sd]?\b",
)


def _matches_any(text, patterns):
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def is_results_entry(title: str, body: str = "") -> bool:
    """True if this feed entry is an actual results release.

    Three gates, in order:

    1. Reject scheduling notices outright. Companies post these weeks ahead
       ("Sets Date of Second-Quarter 2026 Financial Results") and they carry
       every keyword a real release does while announcing nothing.
    2. Require a fiscal period marker in the title -- a quarter or year.
    3. Accept if the title also says results/earnings/reports. If it doesn't,
       fall back to the body and require at least two financial markers.

    Gate 3's fallback exists because titles frequently lead with the story:
    Cerebras's Q2 2026 release was headlined "Fast Inference Cloud Business
    Nearly Quadruples in Second Quarter 2026". Demanding a results word in
    the title would have silently missed it.
    """
    if not title:
        return False
    if _matches_any(title, _EXCLUDE):
        return False
    if not _matches_any(title, _TITLE_PATTERNS):
        return False
    if _matches_any(title, _RESULT_WORDS):
        return True
    hits = sum(1 for p in _FINANCIAL_MARKERS
               if re.search(p, body or "", re.IGNORECASE))
    return hits >= 2


# -- Google News path ------------------------------------------------------
#
# Only 2 of 9 tickers have a working IR feed, and hunting the rest stalled on
# 404s and dead hostnames. Google News covers all nine, is already polled every
# minute by monitor.py, and is proven reachable from the runner -- so it backs
# up the IR feeds rather than adding a new dependency.
#
# The catch is that it carries *media* headlines, not company press releases,
# and they are worded differently. A company writes "Reports Second Quarter
# 2026 Financial Results"; a newsroom writes "Cerebras Q2 revenue tops
# estimates". So the IR classifier's vocabulary does not transfer, and the
# body-fallback is useless here because Google News descriptions are little
# more than the headline repeated.
_MEDIA_RESULT_WORDS = (
    r"\bresults\b",
    r"\bearnings\b",
    r"\breports?\b",
    r"\breported\b",
    r"\bbeats?\b",
    r"\bmisses\b",
    r"\btops\b",
    r"\bposts?\b",
    r"\brevenue\b",
    r"\beps\b",
    r"\bprofit\b",
    r"\bnet loss\b",
)

# Forward-looking coverage is the dangerous class here: it is written *before*
# the release, mentions the quarter, and uses the same verbs. "Analysts expect
# Cerebras to beat Q2 estimates" would otherwise fire the alert a week early
# and end the watch, so nothing would be sent when results actually landed.
_MEDIA_EXCLUDE = (
    r"\bpreview\b",
    r"\bwhat to expect\b",
    r"\bahead of\b",
    r"\bexpect(?:s|ed|ing)?\b",
    r"\banticipat",
    r"\bforecast",
    r"\bestimates? for\b",
    r"\bhow to watch\b",
    r"\boptions? traders?\b",
    r"\bcould\b",
    r"\bwill\b",
    r"\bset to\b",
    r"\bpreviews?\b",
    r"\bupcoming\b",
)


def is_media_results_headline(title: str) -> bool:
    """True if a Google News headline is reporting results that have landed.

    Three gates: reject scheduling notices and forward-looking previews,
    require a fiscal period marker, then require a results word. Unlike
    is_results_entry() there is no body fallback -- Google News gives us
    nothing to fall back to.

    This is strictly the headline test. It runs across every entry in the
    feed, so it only takes one outlet phrasing the story plainly for
    detection to fire, even if others are more creative.
    """
    if not title:
        return False
    if _matches_any(title, _EXCLUDE) or _matches_any(title, _MEDIA_EXCLUDE):
        return False
    if not _matches_any(title, _TITLE_PATTERNS):
        return False
    return _matches_any(title, _MEDIA_RESULT_WORDS)


def _strip_source(title: str) -> str:
    """Google News appends the outlet: "Headline here - Reuters". Drop it, so
    an outlet name never accidentally satisfies a keyword test."""
    return re.sub(r"\s+-\s+[^-]{1,40}$", "", title or "").strip()


def find_release_google(ticker: str, since, state: dict):
    """Look for a results story on Google News, for tickers with no IR feed.

    Imported lazily: monitor.py pulls in yfinance, which is slow to import,
    and this path is only reached when a watch is actually armed.
    """
    from urllib.parse import quote

    from monitor import GOOGLE_NEWS_HEADERS, _news_query

    query = quote(_news_query(ticker, state))
    url = (f"https://news.google.com/rss/search?q={query}"
           f"&hl=en-US&gl=US&ceid=US:en")

    resp = get_with_retry(url, headers=GOOGLE_NEWS_HEADERS, timeout=15,
                          label=f"{ticker} earnings-google")
    if resp is None or not resp.ok:
        print(f"[{ticker}] Google News unavailable this run.")
        return None

    for entry in parse_entries(resp.content):
        title = _strip_source(entry["title"])
        if not is_media_results_headline(title):
            continue
        published = entry["published"]
        if published is not None and since is not None and published < since:
            continue
        return {
            "ticker": ticker.upper(),
            "title": title,
            "link": entry["link"],
            "summary": _clean(entry["summary"], 500),
            "published": published,
            "source": "news",
        }
    return None


def _text(node, *names):
    for name in names:
        found = node.find(name)
        if found is not None and (found.text or "").strip():
            return found.text.strip()
    return ""


def _parse_date(raw):
    """Feeds use RFC-822 (RSS) or ISO-8601 (Atom); accept either."""
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_entries(raw: bytes):
    """Yield {title, link, summary, published} for an RSS or Atom feed."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        print(f"IR feed parse error: {e}")
        return []

    entries = []

    # RSS 2.0
    for item in root.findall("./channel/item"):
        entries.append({
            "title": _text(item, "title"),
            "link": _text(item, "link"),
            "summary": _text(item, "description"),
            "published": _parse_date(_text(item, "pubDate", "date")),
        })

    # Atom (namespaced)
    if not entries:
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for entry in root.findall("./a:entry", ns):
            link = ""
            link_el = entry.find("./a:link", ns)
            if link_el is not None:
                link = link_el.get("href", "")
            entries.append({
                "title": _text(entry, "{http://www.w3.org/2005/Atom}title"),
                "link": link,
                "summary": _text(
                    entry,
                    "{http://www.w3.org/2005/Atom}summary",
                    "{http://www.w3.org/2005/Atom}content",
                ),
                "published": _parse_date(
                    _text(
                        entry,
                        "{http://www.w3.org/2005/Atom}published",
                        "{http://www.w3.org/2005/Atom}updated",
                    )
                ),
            })

    return entries


def _clean(html: str, limit: int = 1200) -> str:
    """Feed descriptions are usually escaped HTML; flatten to readable text."""
    if not html:
        return ""
    text = re.sub(r"<[^>]+>", " ", html)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">")
                .replace("&#39;", "'").replace("&quot;", '"'))
    text = " ".join(text.split())
    return text[:limit].rstrip() + ("..." if len(text) > limit else "")


def find_release_ir(ticker: str, since):
    """Look for a results release on the company's own IR feed."""
    url = FEED_URLS.get(ticker.upper())
    if not url:
        return None

    resp = get_with_retry(url, headers=FEED_HEADERS, timeout=20,
                          label=f"{ticker} ir-feed")
    if resp is None:
        print(f"[{ticker}] IR feed unreachable this run.")
        return None
    if not resp.ok:
        print(f"[{ticker}] IR feed HTTP {resp.status_code}")
        return None

    for entry in parse_entries(resp.content):
        if not is_results_entry(entry["title"], _clean(entry["summary"], 4000)):
            continue
        published = entry["published"]
        if published is not None and since is not None and published < since:
            continue
        return {
            "ticker": ticker.upper(),
            "title": entry["title"],
            "link": entry["link"],
            "summary": _clean(entry["summary"]),
            "published": published,
            "source": "ir",
        }
    return None


def find_release(ticker: str, since, state: dict):
    """Look for a results release published at or after `since`.

    Two sources, IR feed first. The company's own feed is the better one --
    it is authoritative and its description often carries real figures --
    but only 2 of 9 tickers have a reachable one. Google News covers the
    rest, so every ticker gets detection rather than only the lucky two.

    Both are checked even when an IR feed exists: feeds have been observed
    lagging their own website, and a miss costs one extra HTTP call while a
    missed release costs the entire feature.

    `since` bounds the search so a watch armed today doesn't match last
    quarter's release, while still allowing one that lands tomorrow morning
    -- the whole point of the 24-hour window.

    Returns a dict, or None meaning "nothing yet, keep polling".
    """
    release = find_release_ir(ticker, since)
    if release:
        return release
    return find_release_google(ticker, since, state)


def build_message(release: dict) -> str:
    """Telegram body for a detected release. Caller escapes external text."""
    from telegram_utils import escape_markdown

    parts = [f"\U0001F4CA *{release['ticker']} earnings are out*"]
    parts.append("")
    parts.append(escape_markdown(release["title"]))
    if release.get("summary"):
        parts.append("")
        parts.append(escape_markdown(release["summary"]))
    if release.get("link"):
        parts.append("")
        parts.append(release["link"])
    # Say where this came from. A news-sourced alert is a headline someone
    # else wrote about the release, not the release itself -- worth knowing
    # before acting on a number in it.
    if release.get("source") == "news":
        parts.append("")
        parts.append("_(detected via news headline, not the company's own feed)_")
    return "\n".join(parts)
