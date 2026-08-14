"""TEMPORARY probe: can an IR press-release feed be DISCOVERED from a ticker?

The problem this replaces
------------------------
FEED_URLS in ir_feeds.py is hand-maintained and only covers 2 of 9 tickers.
It got that way because earlier rounds GUESSED feed URLs -- assuming every
company used the same IR platform layout as Cerebras
(investors.<company>.com/rss/news-releases.xml). They don't, so seven guesses
failed and the map stalled.

Guessing is the bug. This probe looks things up instead:

    ticker  --yfinance-->  official website
            --scrape---->  the "investor relations" link
            --scrape---->  <link rel="alternate" type="application/rss+xml">
            --fetch----->  does that feed actually answer, with real entries?

If the hit rate is decent, this chain runs once per ticker inside the bot and
the result is cached in state like company names are -- which would make
"earnings for <any ticker>" work with the company's own words rather than a
third-party headline.

Reading round 2 properly
------------------------
Round 2's output looked like total failure but wasn't. The 404s carried real
page bodies:

    GENI  investors.geniussports.com/rss/news-releases.xml  404   1286b
    FOUR  investors.shift4.com/rss/news-releases.xml        404  31682b

A 404 with a body means the SERVER ANSWERED. Those IR hosts are reachable
from the runner; only the path was wrong. Just two (QURE, UAVS) failed DNS,
and those hostnames were invented rather than looked up.

What counts as success
----------------------
Not "a feed URL was found" -- that is how the last map got filled with
non-working URLs. Success is: the feed returns HTTP 200, parses as RSS or
Atom, and yields entries with titles and dates, fetched FROM THE RUNNER. The
last column of the summary is the only one that matters.

Sends nothing, writes nothing. Delete with its workflow once the answer is in.
"""

import re
import sys
from urllib.parse import urljoin, urlparse

import requests
import yfinance as yf

import ir_feeds

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
TIMEOUT = 15

# Your nine, plus arbitrary names you don't hold -- the command accepts any
# ticker, so the probe has to answer for any ticker too.
TICKERS = ["CBRS", "GENI", "EVH", "XPEV", "QURE", "UAVS", "WOLF", "FOUR", "APP",
           "NVDA", "PLTR"]

FEED_LINK_TAG = re.compile(
    r"""<link[^>]+type=["']application/(?:rss|atom)\+xml["'][^>]*>""",
    re.IGNORECASE)
HREF = re.compile(r"""href=["']([^"']+)["']""", re.IGNORECASE)

# Anchors that lead to an investor-relations section. Ordered by how specific
# they are, so "investor relations" beats a bare "investors" in a nav bar.
IR_HINTS = ("investor relations", "investor-relations", "investors", "/investor",
            "ir.", "shareholder")

# Once on an IR site, these subpages are the ones that carry a feed tag when
# the landing page doesn't.
IR_SUBPAGES = ("", "/news-releases", "/news", "/press-releases",
               "/news-events/press-releases", "/overview")


def get(url, label):
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT, allow_redirects=True)
        return r
    except Exception as e:
        print(f"      {label}: {type(e).__name__}")
        return None


def website_for(ticker):
    """yfinance already backs the company-name cache, so this adds no new
    dependency -- just a different field off the same call."""
    try:
        info = yf.Ticker(ticker).info or {}
    except Exception as e:
        print(f"      .info failed: {type(e).__name__}: {e}")
        return None, None
    return (info.get("website") or "").strip(), (info.get("shortName") or "").strip()


ANCHOR = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.IGNORECASE | re.DOTALL)


def find_ir_link(html, base_url):
    """Pick the most promising investor-relations link on a page.

    Matches each anchor's OWN text, bounded by its closing tag. An earlier
    version read a fixed number of characters after the opening tag, which
    ran past the end of the link and into the next ones -- so "About us"
    scored a hit because "Investor Relations" happened to appear 80
    characters later in the footer. Every ticker would have resolved to
    whatever link came first on the page.
    """
    best = None
    best_rank = len(IR_HINTS) + 1
    for attrs, text in ANCHOR.findall(html):
        href_m = HREF.search(attrs)
        if not href_m:
            continue
        href = href_m.group(1)
        # Strip nested markup, then COLLAPSE whitespace. Without the collapse,
        # "<span>Investor</span> <b>Relations</b>" becomes "Investor
        # Relations" with a double space and never matches the hint -- and
        # wrapping link text in spans and icons is the norm on IR sites.
        label = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()
        blob = f"{href} {label}".lower()

        rank = None
        for i, hint in enumerate(IR_HINTS):
            if hint in blob:
                rank = i
                break

        # Sites commonly abbreviate to a bare "IR" link. Matched on exact path
        # SEGMENTS rather than substring, so "/hiring" and "/directory" can't
        # trigger it. Ranked last, so a spelled-out "Investor Relations"
        # elsewhere on the page still wins.
        if rank is None:
            segments = [s for s in urlparse(href).path.lower().split("/") if s]
            if "ir" in segments or label.lower() in ("ir", "ir »", "ir >"):
                rank = len(IR_HINTS)

        if rank is not None and rank < best_rank:
            best, best_rank = urljoin(base_url, href), rank
    return best


def discover_feeds(ir_url):
    """Read the RSS autodiscovery tag rather than guessing a path."""
    feeds = []
    root = f"{urlparse(ir_url).scheme}://{urlparse(ir_url).netloc}"
    for sub in IR_SUBPAGES:
        page = ir_url if sub == "" else urljoin(root + "/", sub.lstrip("/"))
        r = get(page, f"page {page}")
        if r is None or r.status_code != 200:
            if r is not None:
                print(f"      {page} -> {r.status_code}")
            continue
        print(f"      {page} -> 200 ({len(r.text)}b)")
        for tag in FEED_LINK_TAG.findall(r.text):
            hm = HREF.search(tag)
            if hm:
                url = urljoin(page, hm.group(1))
                if url not in feeds:
                    feeds.append(url)
        if feeds:
            break
    return feeds


def validate(feed_url):
    """The only column that matters: does it answer AND parse AND have entries?

    A URL that merely exists is what filled the last map with dead links.
    """
    r = get(feed_url, f"feed {feed_url}")
    if r is None:
        return False, "unreachable", 0
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}", 0
    entries = ir_feeds.parse_entries(r.content)
    if not entries:
        return False, "parsed 0 entries", 0
    titled = [e for e in entries if e.get("title")]
    return bool(titled), f"{len(titled)} entries", len(titled)


def main():
    results = []
    for ticker in TICKERS:
        print("=" * 72)
        print(f"{ticker}")
        print("=" * 72)

        site, name = website_for(ticker)
        print(f"  website: {site or '(none from yfinance)'}   name: {name or '?'}")
        if not site:
            results.append((ticker, "-", "-", "no website", False))
            continue

        home = get(site, "homepage")
        if home is None or home.status_code != 200:
            code = home.status_code if home is not None else "ERR"
            print(f"  homepage -> {code}")
            results.append((ticker, site, "-", f"homepage {code}", False))
            continue

        ir_url = find_ir_link(home.text, site)
        print(f"  IR link: {ir_url or '(none found on homepage)'}")
        if not ir_url:
            results.append((ticker, site, "-", "no IR link", False))
            continue

        feeds = discover_feeds(ir_url)
        print(f"  autodiscovered feeds: {feeds or '(none)'}")
        if not feeds:
            results.append((ticker, site, ir_url, "no feed tag", False))
            continue

        for feed in feeds:
            ok, note, _ = validate(feed)
            print(f"    {feed} -> {note} {'OK' if ok else 'REJECT'}")
            if ok:
                results.append((ticker, site, feed, note, True))
                break
        else:
            results.append((ticker, site, ir_url, "feeds all failed", False))
        print()

    print()
    print("=" * 72)
    print("SUMMARY -- 'usable' means it answered, parsed, and had titled entries")
    print("=" * 72)
    hits = 0
    for ticker, site, feed, note, ok in results:
        hits += 1 if ok else 0
        print(f"  {ticker:5} {'USABLE ' if ok else '       '} {note:20} {feed[:70]}")
    print()
    print(f"  {hits} of {len(results)} tickers yielded a working IR feed.")
    print()
    print("  Paste this back. If the count is high enough to be worth it, the")
    print("  same chain goes into the bot and caches per ticker; if not, we keep")
    print("  the Google News path and stop spending time on IR feeds.")

    # Ready-to-paste map for whatever did work.
    usable = [(t, f) for t, _, f, _, ok in results if ok]
    if usable:
        print()
        print("  FEED_URLS = {")
        for t, f in usable:
            print(f'      "{t}": "{f}",')
        print("  }")


if __name__ == "__main__":
    sys.exit(main())
