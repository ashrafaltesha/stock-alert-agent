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


def _nearest_date(start, end, dates):
    """Closest date OUTSIDE the link's own text.

    Excluding the interior is the whole point. Applied Materials publishes a
    scheduling notice headlined "Applied Materials to Report Fiscal Third
    Quarter 2026 Results on Aug. 13, 2026". Counting dates inside the
    headline dates that notice to the 13th, so on the day Applied Materials
    actually reported, the bot sent the weeks-old announcement of the date
    instead of the results. With the interior excluded it correctly returns
    "Applied Materials Announces Third Quarter 2026 Results".

    A publication date is always rendered beside a headline, never inside it,
    so nothing legitimate is lost.
    """
    if not dates:
        return None
    best, best_gap = None, None
    for at, d in dates:
        if start <= at <= end:
            continue
        gap = min(abs(at - start), abs(at - end))
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

        when = _date_in_url(url) or _nearest_date(m.start(), m.end(), dates)
        if not when:
            continue

        seen.add(url)
        out.append({"title": title, "url": url, "date": when})

    return out


# An IR landing page is an overview -- latest stock price, a promo panel,
# maybe one headline. The press releases live on a news subpage. Applied
# Materials' landing page yielded 4 links and none of them the results;
# /news-releases yielded 10 including it. Applied Industrial, QXO and Madison
# Square Garden Sports all returned nothing at all from their landing pages.
NEWS_SUBPAGES = ("/news-releases", "/news", "/press-releases",
                 "/news-events/news-releases", "/news-events/press-releases")


def _fetch_one(url, label):
    resp = get_with_retry(url, headers=PAGE_HEADERS, timeout=20, label=label)
    if resp is None or not resp.ok:
        code = resp.status_code if resp is not None else "unreachable"
        print(f"[{label}] {url} -> {code}")
        return None
    return resp


def fetch_articles(ir_url: str, label: str = "ir-page"):
    """Fetch the IR page -- and its news subpage -- and extract dated links.

    Tries the news subpages first and only falls back to the landing page,
    because that ordering is what the live test demanded: four of the ten
    companies checked had a landing page with no press releases on it.

    Returns [] on any failure -- unreachable, blocked, or a JavaScript shell
    with nothing in it. An empty list means "nothing to see", which is also
    the right answer for a bot-protected site.
    """
    root = f"{urlparse(ir_url).scheme}://{urlparse(ir_url).netloc}"
    tried = []

    for path in NEWS_SUBPAGES:
        candidate = root + path
        if candidate in tried:
            continue
        tried.append(candidate)
        resp = _fetch_one(candidate, label)
        if resp is None:
            continue
        articles = extract_articles(resp.text, candidate)
        if articles:
            print(f"[{label}] {len(articles)} dated articles from {candidate}")
            return articles

    resp = _fetch_one(ir_url, label)
    if resp is None:
        return []
    articles = extract_articles(resp.text, ir_url)
    if articles:
        print(f"[{label}] {len(articles)} dated articles from {ir_url}")
    else:
        # Distinguishing a shell from an empty page matters when reading logs:
        # a big body with no articles means JavaScript-rendered or blocked.
        print(f"[{label}] no dated articles in {len(resp.text)} bytes at "
              f"{ir_url} (likely JavaScript-rendered or bot-protected).")
    return articles


# -- Discovery: ticker -> investor-relations page ---------------------------

# Anchors that lead to an IR section, most specific first so a spelled-out
# "Investor Relations" beats a bare "Investors" nav item.
_IR_HINTS = ("investor relations", "investor-relations", "investors",
             "/investor", "shareholder")

_ANCHOR_HREF = re.compile(r"""href=["']([^"']+)["']""", re.IGNORECASE)


def _find_ir_link(html, base_url):
    """Pick the most promising investor-relations link on a homepage.

    Matches each anchor's OWN text, bounded by its closing tag. Reading a
    fixed number of characters after the opening tag instead runs past the
    end of the link into the next ones, so "About us" scores a hit because
    "Investor Relations" appears further down the footer -- which would send
    every ticker to whatever link came first on the page.
    """
    best, best_rank = None, len(_IR_HINTS) + 1
    for m in ANCHOR.finditer(html):
        href_m = _ANCHOR_HREF.search(m.group(1))
        if not href_m:
            continue
        href = href_m.group(1)
        # Strip nested markup then COLLAPSE whitespace: "<span>Investor</span>
        # <b>Relations</b>" otherwise becomes "Investor  Relations" with a
        # double space and never matches -- and wrapping link text in spans
        # and icons is the norm on corporate sites.
        label = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(2))).strip()
        blob = f"{href} {label}".lower()

        rank = next((i for i, h in enumerate(_IR_HINTS) if h in blob), None)
        if rank is None:
            # Sites commonly abbreviate to a bare "IR". Matched on exact path
            # SEGMENTS so "/hiring" and "/directory" can't trigger it.
            segs = [s for s in urlparse(href).path.lower().split("/") if s]
            if "ir" in segs or label.lower() == "ir":
                rank = len(_IR_HINTS)
        if rank is None:
            continue

        # A dedicated IR HOST beats a marketing page every time. Brookfield's
        # homepage links "Investors" to /invest-with-us/private-wealth, a
        # sales page with no press releases -- while investors.brookfield.com
        # is the real thing. Hostname is the stronger signal, so it wins even
        # against a better-worded link.
        absolute = urljoin(base_url, href)
        host = urlparse(absolute).netloc.lower()
        if host.startswith(("ir.", "investor.", "investors.")):
            rank -= 100

        if rank < best_rank:
            best, best_rank = absolute, rank
    return best


def discover_ir_page(ticker: str, state: dict):
    """Resolve a ticker to its investor-relations page URL, cached in state.

    Cached indefinitely and deliberately: the chain below costs a slow
    yfinance .info call plus a homepage fetch, and the polling loop runs once
    a minute. Resolving it every time would dominate the run.

    An empty result is cached too, so a company with no findable IR page
    doesn't re-trigger the whole chain every minute forever. Delete the
    ir_page::TICKER key to force a fresh lookup.
    """
    key = f"ir_page::{ticker.upper()}"
    if key in state:
        return state[key] or None

    url = ""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
        site = (info.get("website") or "").strip()
    except Exception as e:
        print(f"[{ticker}] website lookup failed: {type(e).__name__}: {e}")
        site = ""

    if site:
        resp = get_with_retry(site, headers=PAGE_HEADERS, timeout=20,
                              label=f"{ticker} homepage")
        if resp is not None and resp.ok:
            url = _find_ir_link(resp.text, site) or ""
        else:
            # A 429 here means rate-limited, not absent -- don't poison the
            # cache with a miss that a later attempt would resolve.
            code = resp.status_code if resp is not None else "unreachable"
            print(f"[{ticker}] homepage {code}; not caching a miss.")
            return None

    state[key] = url
    print(f"[{ticker}] IR page resolved to: {url or '(none found)'}")
    return url or None


def articles_for(ticker: str, state: dict):
    """Best available source of dated articles for a ticker.

    A preference ladder, not a category test -- a site can be both feed-backed
    and HTML-readable, and the more structured source wins:

        1. RSS feed, where one exists (cleanest dates, explicit timestamps)
        2. The IR page's HTML (works for most; no feed required)

    Returns (articles, source). An empty list means neither worked, and the
    caller falls back to news headlines. That is the honest outcome for sites
    like AppLovin and Genius Sports, which serve nothing to an automated
    reader even in a real browser.
    """
    from ir_feeds import FEED_URLS, FEED_HEADERS, parse_entries

    feed_url = FEED_URLS.get(ticker.upper())
    if feed_url:
        resp = get_with_retry(feed_url, headers=FEED_HEADERS, timeout=20,
                              label=f"{ticker} ir-feed")
        if resp is not None and resp.ok:
            out = []
            for e in parse_entries(resp.content):
                published = e.get("published")
                if e.get("title") and e.get("link") and published:
                    out.append({"title": e["title"], "url": e["link"],
                                "date": published.date()})
            if out:
                return out, "feed"

    ir_url = discover_ir_page(ticker, state)
    if ir_url:
        articles = fetch_articles(ir_url, label=ticker)
        if articles:
            return articles, "page"

    return [], None


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
