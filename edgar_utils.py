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

FTS_URL = "https://efts.sec.gov/LATEST/search-index"
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


def _get_json(url, params=None):
    resp = get_with_retry(url, headers=SEC_HEADERS, params=params, timeout=20,
                          label="edgar")
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


def find_results_filing(ticker: str, target_date: str):
    """Locate a results filing for `ticker` on `target_date` via EDGAR
    full-text search.

    This deliberately avoids www.sec.gov. That host returns HTTP 403 to
    GitHub Actions runners -- SEC blocks datacenter IP ranges, so the
    ticker->CIK map at /files/company_tickers.json is unreachable from CI
    even with a valid User-Agent. efts.sec.gov is separate infrastructure
    and, usefully, its response already carries everything we need: the
    CIK, the accession number, the item codes, and the exhibit filename.

    Returns a dict describing the press-release exhibit, or None.
    """
    params = {
        "q": f'"{ticker}"',
        "forms": "8-K,6-K",
        "startdt": target_date,
        "enddt": target_date,
    }
    data = _get_json(FTS_URL, params=params)
    if not data:
        return None

    hits = ((data.get("hits") or {}).get("hits")) or []
    best = None
    for hit in hits:
        src = hit.get("_source") or {}
        names = " ".join(src.get("display_names") or []).upper()
        # Full-text search matches any document mentioning the symbol, so
        # confirm the filer really is this ticker rather than someone who
        # merely referenced it.
        if f"({ticker.upper()})" not in names:
            continue

        items = src.get("items") or []
        form = src.get("form") or src.get("root_forms", [""])[0]
        if form == "8-K" and EARNINGS_ITEM not in items:
            continue

        doc_id = hit.get("_id") or ""
        accession, _, filename = doc_id.partition(":")
        ciks = src.get("ciks") or []
        if not (accession and filename and ciks):
            continue

        candidate = {
            "form": form,
            "accession": accession,
            "filename": filename,
            "cik": ciks[0],
            "file_type": src.get("file_type") or "",
        }
        # EX-99.1 is the press release itself; the bare 8-K is just the
        # cover page, so prefer the exhibit when both come back.
        if candidate["file_type"].upper().startswith("EX-99"):
            return candidate
        if best is None:
            best = candidate

    return best

def _html_to_text(raw: bytes) -> str:
    try:
        import lxml.html

        doc = lxml.html.fromstring(raw)
        return doc.text_content()
    except Exception:
        text = raw.decode("utf-8", "replace")
        return re.sub(r"<[^>]+>", " ", text)


def fetch_release_text(filing: dict):
    """Download the exhibit FTS identified for this filing."""
    acc_nodash = filing["accession"].replace("-", "")
    url = (
        ARCHIVE_BASE.format(cik=str(int(filing["cik"])), acc=acc_nodash)
        + "/" + filing["filename"]
    )
    resp = get_with_retry(url, headers=SEC_HEADERS, timeout=20, label="edgar-doc")
    if resp is None:
        print(f"EDGAR: could not fetch {url}")
        return None
    if not resp.ok:
        print(f"EDGAR HTTP {resp.status_code} fetching exhibit {url}")
        return None
    return _html_to_text(resp.content)

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
    been filed yet -- None simply means "keep polling". `state` is accepted
    for interface stability (and future caching) but no longer needed for a
    CIK lookup, since full-text search returns the CIK directly.
    """
    filing = find_results_filing(ticker, target_date)
    if not filing:
        return None

    text = fetch_release_text(filing)
    if not text:
        print(f"[{ticker}] EDGAR: {filing['form']} found but exhibit unreadable.")
        return None

    low = text.lower()
    if not any(h in low for h in _RESULTS_HINTS):
        # Guards against a 6-K (no item codes) that is some other
        # disclosure, and against an 8-K cover page with no figures.
        return None

    acc_nodash = filing["accession"].replace("-", "")
    url = (
        ARCHIVE_BASE.format(cik=str(int(filing["cik"])), acc=acc_nodash)
        + f"/{filing['accession']}-index.htm"
    )
    print(f"[{ticker}] EDGAR: found {filing['form']} {filing['accession']}")
    return {
        "form": filing["form"],
        "accession": filing["accession"],
        "url": url,
        "summary": summarize_release(text),
    }
