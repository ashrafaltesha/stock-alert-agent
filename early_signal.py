"""A headline-based early warning for companies whose SEC filing lags.

The problem
-----------
SEC filings are the right source of record for earnings: the SEC labels them,
they are complete, and they cannot be spoofed by a content farm. But being
authoritative is not the same as being first.

Domestic issuers furnish results on an 8-K item 2.02 essentially at the same
moment as the press release, so EDGAR is joint-first and nothing is lost.
Foreign private issuers have no equivalent obligation. XPeng announced Q2 2026
on the morning of 2026-08-24; by mid-morning the only 6-K on EDGAR was an
unrelated transaction notice. Waiting on the filing meant knowing hours after
the market did.

What this does
--------------
While a watch is ALREADY ARMED, and only for companies that file as foreign
private issuers, it watches Google News for a wire-service headline announcing
results. If one appears, it sends a link-only heads-up and keeps watching
EDGAR for the actual figures.

Why this is not the old system
------------------------------
Earnings detection used to run on news headlines and it was replaced for good
reasons: dozens of articles mention a ticker daily, headlines lie about what
they contain, and Cerebras titled its Q2 release "Fast Inference Cloud
Business Nearly Quadruples in Second Quarter 2026" with no results word in it.

Three constraints keep this from repeating that:

1. IT IS NEVER THE DETECTOR. It cannot report figures and cannot resolve a
   watch. The SEC filing still does both. This only says "results appear to
   be out, here is the link".
2. IT ONLY RUNS WHILE ARMED. On a day nothing is expected, it makes no
   requests and can produce nothing. The calendar-armed watch is the gate.
3. WIRE SERVICES ONLY. Business Wire, PR Newswire, GlobeNewswire, Reuters and
   the majors -- outlets that republish a company's own release. Aggregators
   and commentary cannot trigger it, so "XPeng earnings preview" from a
   content farm is invisible to it.

Sending one extra message on a day a company was already expected to report
is a small cost. Learning about results hours late is not.

Standard library only, so the watcher still needs no pip install.
"""

import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

TIMEOUT = 12

# Outlets that carry a company's own release rather than commentary about it.
# Deliberately narrow: this list is the entire defence against the failure
# mode that got headline-based detection retired in the first place.
WIRE_SOURCES = (
    "business wire", "businesswire",
    "pr newswire", "prnewswire",
    "globenewswire", "globe newswire",
    "accesswire", "newsfile",
    "reuters", "bloomberg", "associated press", "ap news",
    "cnbc", "marketwatch", "barron's", "financial times",
    "seeking alpha",          # carries verbatim release text for FPIs
    "investing.com",
)

# Phrasings a results announcement actually uses. Requires an explicit results
# word AND a period reference, because "XPeng announces new model" and
# "XPeng reports record deliveries" are not earnings.
_RESULTS_RE = re.compile(
    r"\b(?:reports?|announces?|posts?|releases?)\b[^.]{0,60}\b"
    r"(?:results|earnings|financial results|unaudited)\b"
    r"|\b(?:first|second|third|fourth)[- ]quarter\b[^.]{0,40}\b(?:results|earnings)\b"
    r"|\bq[1-4]\s*(?:20\d\d|fy\d\d)?\s*(?:results|earnings)\b"
    r"|\b(?:full[- ]year|annual|interim)\b[^.]{0,30}\bresults\b",
    re.IGNORECASE)

_PERIOD_RE = re.compile(
    r"\b(?:q[1-4]|first|second|third|fourth|half|full[- ]year|annual|interim|"
    r"20\d\d|fiscal)\b", re.IGNORECASE)


# Checked FIRST, because these all contain the same words a real
# announcement does.
#
# The scheduling notice is the dangerous one and it has caught this project
# out before: "XPeng to report second quarter results on August 24" contains
# "report", "second quarter" and "results", and is published DAYS EARLY. The
# IR-page scraper made exactly this mistake with Applied Materials and dated
# a notice as the release. Future tense is the whole difference.
_NOT_RESULTS_RE = re.compile(
    r"\b(?:to|will|set to|scheduled to|plans to|expects to|is to)\s+"
    r"(?:report|announce|release|post|publish)\b"
    r"|\b(?:preview|previews|what to (?:watch|expect)|ahead of|"
    r"expectations|estimates|forecast|outlook for|analysts? expect)\b"
    r"|\bearnings (?:call|date|preview|season)\b"
    r"|\b(?:to host|to hold|will host)\b",
    re.IGNORECASE)


def looks_like_results(title: str) -> bool:
    """A published results announcement, not a plan to publish one."""
    title = title or ""
    if _NOT_RESULTS_RE.search(title):
        return False
    return bool(_RESULTS_RE.search(title) and _PERIOD_RE.search(title))


def source_is_wire(source: str) -> bool:
    lowered = (source or "").lower()
    return any(w in lowered for w in WIRE_SOURCES)


def _fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; personal-stock-alerts)",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def find_results_headline(ticker: str, company_name: str = ""):
    """Returns {"title","source","link"} for a wire results story, or None.

    Searched by company name where known -- the same lesson monitor.py
    learned, since "FOUR stock" and "APP stock" are ordinary English and
    return mostly unrelated material.
    """
    query = f'"{company_name}"' if company_name else f"{ticker} stock"
    url = ("https://news.google.com/rss/search?q="
           + urllib.parse.quote(f"{query} results")
           + "&hl=en-US&gl=US&ceid=US:en")

    try:
        root = ET.fromstring(_fetch(url))
    except (urllib.error.URLError, ET.ParseError, OSError) as e:
        # Never fatal. This is an accelerant on top of SEC detection, and
        # failing it must not disturb the loop that does the real work.
        print(f"[{ticker}] early-signal fetch failed (non-fatal): "
              f"{type(e).__name__}: {e}")
        return None

    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        source_el = item.find("source")
        source = (source_el.text if source_el is not None else "") or ""

        # Google appends " - Publisher" to the title; that suffix is also
        # the only source name available on some items.
        suffix = title.rsplit(" - ", 1)[1] if " - " in title else ""

        if not (source_is_wire(source) or source_is_wire(suffix)):
            continue
        if not looks_like_results(title):
            continue
        return {
            "title": title,
            "source": source or suffix,
            "link": (item.findtext("link") or "").strip(),
        }
    return None
