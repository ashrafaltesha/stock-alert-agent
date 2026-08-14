"""Read a company's investor-relations page and pull out dated article links.

Why this exists
---------------
Detection used to depend on a hand-written map of RSS feed URLs, which only
ever covered 2 of 9 tickers. A probe that discovered feeds properly -- by
reading the <link rel="alternate"> tag rather than guessing paths -- still
only found 2 working feeds out of 11. Most IR sites simply don't publish one.

But the same probe found the IR *page* for 9 of 11, and inspecting what those
pages actually return to a plain HTTP client showed the data is usually right
there in the HTML:

    CBRS  63KB   "August 12, 2026" + /news-releases/news-release-details/...
    EVH   1.7MB  date embedded in the URL: /2026-08-06-Evolent-Announces-...
    QURE  76KB   6 dated entries
    WOLF  97KB   nothing -- Q4 platform shell, data arrives by JSON API
    GENI  72KB   nothing -- reCAPTCHA, blocked even in a real browser

So: parse the page. The markup differs per IR platform, but the shape never
does -- a link to the article with its date within a few hundred characters,
or the date embedded in the URL itself.

The detection rule
------------------
Not keyword matching: anything the company posts dated today. On the day a
company reports, the release is essentially the only thing it publishes, so
the date is a far more reliable signal than whether a headline sounds like
earnings -- the keyword classifier this replaces nearly missed the real
Cerebras release, whose headline contained no results word at all.

Everything from that day counts, including items posted before the command
was sent, because the goal is awareness of everything the company says on its
reporting day rather than just the results document.

That makes the watch a running feed rather than a one-shot trigger, and the
caller must not close it on the first match -- otherwise a routine morning
announcement would end the watch and the actual results, hours later, would
never arrive. Sent URLs are remembered so nothing repeats.
"""

import re
from datetime import date
from urllib.parse import urljoin, urlparse

from http_utils import get_with_retry

PAGE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
}

ANCHOR = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.IGNORECASE | re.DOTALL)
HREF = re.compile(r"""href=["']([^"']+)["']""", re.IGNORECASE)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# "August 12, 2026", "Aug 6, 2026", "Aug. 6 2026" -- IR platforms use all of
# these, sometimes on the same site.
TEXT_DATE = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)\.?\s+(\d{1,2}),?\s+(20\d\d)\b",
    re.IGNORECASE)

ISO_DATE = re.compile(r"\b(20\d\d)-(\d{2})-(\d{2})\b")

# Links that are never a press release, however close a date happens to sit.
_SKIP_URL = re.compile(
    r"(?:\.pdf$|\.jpg$|\.png$|/rss|/feed|mailto:|tel:|javascript:|"
    r"/search|/subscribe|/email-alerts|/contact|#)", re.IGNORECASE)

# How far from a link a date may sit and still be considered its date. The
# real markup keeps them within a few hundred characters; anything further is
# a different article's date, or page furniture.
PROXIMITY = 600


def _parse_text_date(m):
    month = _MONTHS.get(m.group(1)[:3].lower())
    try:
        return date(int(m.group(3)), month, int(m.group(2))) if month else None
    except ValueError:
        return None


def _parse_iso(m):
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _date_positions(html):
    """Every date in the document, with where it sits, so links can be paired
    with the nearest one."""
    found = []
    for m in TEXT_DATE.finditer(html):
        d = _parse_text_date(m)
        if d:
            found.append((m.start(), d))
    for m in ISO_DATE.finditer(html):
        d = _parse_iso(m)
        if d:
            found.append((m.start(), d))
    found.sort()
    return found


def _date_in_url(url):
    """Some platforms put the date straight in the path -- Evolent publishes
    /2026-08-06-Evolent-Announces-Second-Quarter-2026-Results. When present
    this beats proximity, since it can't be confused with a neighbour."""
    m = ISO_DATE.search(url)
    return _parse_iso(m) if m else None


def _nearest_date(pos, dates):
    if not dates:
        return None
    best, best_gap = None, None
    for at, d in dates:
        gap = abs(at - pos)
        if best_gap is None or gap < best_gap:
            best, best_gap = d, gap
    return best if best_gap is not None and best_gap <= PROXIMITY else None


def extract_articles(html: str, base_url: str):
    """Return [{title, url, date}] for every dated article link on the page.

    Deliberately generic. Rather than a parser per IR platform -- which would
    break the first time any of them redesigned -- this pairs each link with
    the closest date in the document, which is what every platform's markup
    amounts to once the class names are stripped away.
    """
    dates = _date_positions(html)
    seen = set()
    out = []

    for m in ANCHOR.finditer(html):
        attrs, text = m.group(1), m.group(2)
        href_m = HREF.search(attrs)
        if not href_m:
            continue
        href = href_m.group(1).strip()
        if not href or _SKIP_URL.search(href):
            continue

        url = urljoin(base_url, href)
        if url in seen:
            continue

        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()
        if len(title) < 15:
            # Navigation ("News", "Read more") rather than a headline.
            continue

        when = _date_in_url(url) or _nearest_date(m.start(), dates)
        if not when:
            continue

        seen.add(url)
        out.append({"title": title, "url": url, "date": when})

    return out


def fetch_articles(ir_url: str, label: str = "ir-page"):
    """Fetch the IR page and extract its dated article links.

    Returns [] on any failure -- unreachable, blocked, or a JavaScript shell
    with nothing in it. The caller treats an empty list as "nothing to see",
    which is also the right answer when a page is bot-protected.
    """
    resp = get_with_retry(ir_url, headers=PAGE_HEADERS, timeout=20, label=label)
    if resp is None:
        print(f"[{label}] IR page unreachable this run.")
        return []
    if not resp.ok:
        print(f"[{label}] IR page HTTP {resp.status_code}")
        return []

    articles = extract_articles(resp.text, ir_url)
    if not articles:
        # Distinguishing a shell from an empty page matters when reading logs:
        # a big body with no articles means JavaScript-rendered or blocked.
        print(f"[{label}] no dated articles found in {len(resp.text)} bytes "
              f"(likely JavaScript-rendered or bot-protected).")
    return articles


def todays_articles(articles, already_sent_urls, today: date):
    """The detection rule: anything dated today that hasn't been sent yet.

    Deliberately not keyword-matched. On the day a company reports, the
    release is essentially the only thing it posts, so the date is a far more
    reliable signal than whether a headline sounds like earnings -- the
    keyword classifier this replaces nearly missed the real Cerebras release,
    whose headline contained no results word at all.

    Deliberately not restricted to items published after the watch was armed,
    either. The point is awareness of everything the company says on its
    reporting day, including anything posted that morning before the command
    was sent.

    Two consequences the caller has to honour:

    1. The watch must NOT end on the first match. A routine morning
       announcement would otherwise close the watch and the actual results,
       hours later, would never be sent.
    2. `already_sent_urls` must persist across runs, or the same article is
       re-sent every polling cycle -- roughly once a minute.
    """
    return [a for a in articles
            if a["url"] not in already_sent_urls and a["date"] == today]
