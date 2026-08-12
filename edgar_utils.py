"""Earnings-release detection straight from SEC EDGAR.

Why this exists: the on-demand "earnings for TICKER" flow used to rely
solely on Finnhub's calendar populating an `epsActual` value. That can lag
the actual release by hours -- for CBRS on 2026-08-12 the 8-K was on EDGAR
within minutes of the close while Finnhub was still empty, so no alert went
out.

EDGAR is the primary source: a company's earnings release IS the 8-K it
files under Item 2.02 (Results of Operations and Financial Condition), with
the press release attached as exhibit EX-99.1. It's free, needs no API key,
and appears the moment the company files.

Foreign private issuers (GENI, XPEV among your holdings) file 6-K instead of
8-K. 6-Ks carry no item codes, so those are matched on content instead.

SEC requires a descriptive User-Agent on every request; unidentified traffic
gets blocked. See https://www.sec.gov/os/accessing-edgar-data
"""

import json
import re

from http_utils import get_with_retry

SEC_HEADERS = {
    "User-Agent": "stock-alert-agent ashrafaltesha@users.noreply.github.com",
    "Accept-Encoding": "gzip, deflate",
}

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
ARCHIVE_BASE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}"

# Item 2.02 == "Results of Operations and Financial Condition". This is the
# item code a company uses when it publishes quarterly results.
EARNINGS_ITEM = "2.02"

# Used to confirm a candidate exhibit really is the results release, and to
# decide whether a 6-K (which has no item codes) is an earnings filing.
_RESULTS_HINTS = (
    "financial results",
    "first quarter",
    "second quarter",
    "third quarter",
    "fourth quarter",
    "full year",
    "fiscal year",
    "quarterly results",
    "results of operations",
    "earnings",
)

# Where the useful part of a press release stops.
_STOP_MARKERS = (
    "about ",
    "forward-looking",
    "forward looking",
    "conference call",
    "earnings webcast",
    "webcast",
    "investor relations",
    "media relations",
    "non-gaap",
    "use of non-gaap",
)

_BULLET_CHARS = ("\u2022", "\u25cf", "\u00b7", "-", "*")


def _get_json(url):
    resp = get_with_retry(url, headers=SEC_HEADERS, timeout=20, label="edgar")
    if resp is None:
        print(f"EDGAR request failed outright: {url}")
        return None
    if not resp.ok:
        # Log the status explicitly. A silent None here made an SEC 403
        # (which is what happens when the User-Agent isn't accepted) look
        # identical to "nothing filed yet", which is very misleading.
        print(f"EDGAR HTTP {resp.status_code} for {url} -- body: {resp.text[:200]}")
        return None
    try:
        return resp.json()
    except (ValueError, json.JSONDecodeError) as e:
        print(f"EDGAR returned non-JSON from {url}: {e}")
        return None


def get_cik(ticker: str, state: dict):
    """Resolve a ticker to its zero-padded 10-digit CIK, cached in state.

    The full ticker->CIK map is a single ~1MB file, so we fetch it once and
    cache each resolved ticker rather than re-downloading per lookup.
    """
    key = f"edgar_cik::{ticker.upper()}"
    if key in state:
        return state[key] or None

    data = _get_json(TICKER_MAP_URL)
    if not data:
        print(f"[{ticker}] EDGAR: ticker map unavailable.")
        return None

    found = None
    for row in data.values():
        if str(row.get("ticker", "")).upper() == ticker.upper():
            found = str(row.get("cik_str", "")).zfill(10)
            break

    # Cache misses too, so an unlisted symbol doesn't refetch the map every run.
    state[key] = found or ""
    if found:
        print(f"[{ticker}] EDGAR CIK resolved: {found}")
    else:
        print(f"[{ticker}] EDGAR: no CIK found (not a US-listed filer?).")
    return found


def find_results_filing(cik: str, target_date: str):
    """Return the most recent 8-K (Item 2.02) or 6-K filed on target_date.

    target_date is 'YYYY-MM-DD'. Returns a dict with accession/primary doc,
    or None if the company hasn't filed results that day.
    """
    data = _get_json(SUBMISSIONS_URL.format(cik10=cik))
    if not data:
        return None

    recent = (data.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    accessions = recent.get("accessionNumber") or []
    primaries = recent.get("primaryDocument") or []
    items = recent.get("items") or []

    for idx, form in enumerate(forms):
        if dates[idx : idx + 1] and dates[idx] != target_date:
            continue
        item_str = items[idx] if idx < len(items) else ""
        if form == "8-K":
            if EARNINGS_ITEM not in (item_str or ""):
                continue
        elif form != "6-K":
            continue
        return {
            "form": form,
            "accession": accessions[idx],
            "primary": primaries[idx] if idx < len(primaries) else "",
            "items": item_str,
        }
    return None


def _html_to_text(raw: bytes) -> str:
    try:
        import lxml.html

        doc = lxml.html.fromstring(raw)
        return doc.text_content()
    except Exception:
        text = raw.decode("utf-8", "replace")
        return re.sub(r"<[^>]+>", " ", text)


def fetch_release_text(cik: str, filing: dict):
    """Pull the press-release exhibit text for a filing.

    The exhibit isn't reliably named (CBRS filed it as
    'cbrsannouncesfinancialresu.htm', not 'ex99-1.htm'), so instead of
    matching filenames we take the non-primary .htm documents largest-first
    and keep the one that actually reads like a results release.
    """
    acc_nodash = filing["accession"].replace("-", "")
    base = ARCHIVE_BASE.format(cik=str(int(cik)), acc=acc_nodash)

    listing = _get_json(f"{base}/index.json")
    if not listing:
        return None

    entries = ((listing.get("directory") or {}).get("item")) or []
    candidates = []
    for entry in entries:
        name = entry.get("name", "")
        if not name.lower().endswith((".htm", ".html")):
            continue
        if name == filing.get("primary"):
            continue
        try:
            size = int(entry.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        candidates.append((size, name))

    # Largest first -- the results release is invariably the longest exhibit.
    for _size, name in sorted(candidates, reverse=True):
        resp = get_with_retry(f"{base}/{name}", headers=SEC_HEADERS, timeout=20,
                              label="edgar-doc")
        if resp is None or not resp.ok:
            continue
        text = _html_to_text(resp.content)
        low = text.lower()
        if any(h in low for h in _RESULTS_HINTS):
            return text
    return None


def summarize_release(text: str, max_bullets: int = 14, max_chars: int = 2800) -> str:
    """Condense a press release into headline + its own highlight bullets.

    Earnings releases lead with the figures the company considers most
    important, so rather than trying to interpret the financials we surface
    the issuer's own summary bullets and stop at the boilerplate.
    """
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]

    headline = ""
    for ln in lines[:40]:
        if 25 <= len(ln) <= 220:
            headline = ln
            break

    bullets = []
    for ln in lines:
        low = ln.lower()
        if any(low.startswith(m) or low == m.strip() for m in _STOP_MARKERS):
            break
        stripped = ln.lstrip("".join(_BULLET_CHARS)).strip()
        if not stripped or stripped == ln:
            continue
        if len(stripped) < 12:
            continue
        if len(stripped) > 240:
            stripped = stripped[:237] + "..."
        if stripped not in bullets:
            bullets.append(stripped)
        if len(bullets) >= max_bullets:
            break

    parts = []
    if headline:
        parts.append(headline)
    parts.extend(f"- {b}" for b in bullets)
    out = "\n".join(parts).strip()
    if len(out) > max_chars:
        out = out[:max_chars].rstrip() + "..."
    return out


def get_earnings_release_edgar(ticker: str, target_date: str, state: dict):
    """Detect and summarize a results filing for ticker on target_date.

    Returns {"form", "accession", "url", "summary"} or None if nothing has
    been filed yet -- None simply means "keep polling".
    """
    cik = get_cik(ticker, state)
    if not cik:
        return None

    filing = find_results_filing(cik, target_date)
    if not filing:
        return None

    text = fetch_release_text(cik, filing)
    if not text:
        print(f"[{ticker}] EDGAR: {filing['form']} found but no readable exhibit yet.")
        return None

    if filing["form"] == "6-K":
        # 6-Ks cover any material foreign-issuer disclosure, so confirm this
        # one is actually results before alerting on it.
        low = text.lower()
        if not any(h in low for h in _RESULTS_HINTS):
            return None

    acc_nodash = filing["accession"].replace("-", "")
    url = (
        ARCHIVE_BASE.format(cik=str(int(cik)), acc=acc_nodash)
        + f"/{filing['accession']}-index.htm"
    )
    return {
        "form": filing["form"],
        "accession": filing["accession"],
        "url": url,
        "summary": summarize_release(text),
    }
