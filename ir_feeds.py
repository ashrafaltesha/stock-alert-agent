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
}

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


def find_release(ticker: str, since):
    """Look for a results release published at or after `since`.

    `since` bounds the search so a watch armed today doesn't match last
    quarter's release, while still allowing a release that lands tomorrow
    morning -- which is the whole point of the 24-hour window.

    Returns a dict, or None meaning "nothing yet, keep polling".
    """
    url = FEED_URLS.get(ticker.upper())
    if not url:
        print(f"[{ticker}] no IR feed configured -- cannot detect earnings.")
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
        }
    return None


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
    return "\n".join(parts)
