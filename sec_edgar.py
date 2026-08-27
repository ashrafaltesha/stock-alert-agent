"""Earnings detection from SEC filings.

Standard library only, deliberately. The polling loop that uses this runs
every 15 seconds, and the old workflow spent 20-40 seconds of every run
installing yfinance and lxml before doing 2 seconds of work. Nothing here
needs a third-party package, so that entire cost disappears.

Why filings rather than the company's own website
-------------------------------------------------
Earlier versions read investor-relations pages. That worked for about half
the holdings and failed for the rest -- Genius Sports serves a reCAPTCHA,
AppLovin returns an empty shell, and several others are JavaScript apps that
give an automated reader nothing. No amount of parsing fixes a site that
declines to be read.

Filing with the SEC is a legal obligation, so coverage is universal. Both of
those companies file on time, every quarter.

The detection rules, and the evidence for them
----------------------------------------------
DOMESTIC filers (8-K): item 2.02 is "Results of Operations and Financial
Condition". The SEC labels the earnings release itself, so there is nothing
to infer. Measured across 166 filings from 15 companies, this never once
mislabelled anything.

FOREIGN filers (6-K): no item codes exist, so the filing's own text has to
decide. Every cheaper signal was tested against 40 foreign issuers and every
one failed:

    isXBRLNumeric   0 for all 344 of Alibaba's 6-Ks
    reportDate      mirrors the filing date, says nothing
    file size       Alibaba's earnings exhibit 22k, a routine one 10k
    filename        Genius Sports names them q2_26; XPeng names everything
                    dNNNNNNd6k.htm
    attachment type the index.json "type" field is a UI icon, not a category

What does work is counting financial-statement terms in the exhibits. Across
40 foreign issuers the separation was stark: earnings filings scored 5-14,
routine ones 0-4, with almost nothing in between.

FILING_SCORE_MIN is 5 rather than 7 because of Honda specifically. Its
quarterly filings score 5-6 every single quarter (Aug 5, May 14, Feb 10,
Nov 7), while an earlier sample of 20 issuers suggested 7 was safe. Honda
alone would have been missed four times a year. Precision at 5 is still very
high: HSBC files daily buyback notices and produced exactly one hit in 45
filings; Li Auto, one in 26.

Known gap: Sea Limited files bare 1,418-character cover pages with no exhibit
at all. There is no content to read, so no content rule can catch it.
"""

import gzip
import json
import os
import re
import urllib.error
import urllib.request
import zlib

# The SEC asks automated clients to identify themselves with a contact
# address. Both this and a generic agent return 200 from GitHub runners, but
# declaring is the condition of continued access, so it is not optional.
USER_AGENT = "ashrafaltesha personal-stock-alerts altesha@outlook.com"

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}"
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"

CIK_MAP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "cik_map.json")

# 8-K item code for "Results of Operations and Financial Condition".
EARNINGS_ITEM = "2.02"

# Financial-statement vocabulary. A results release contains most of these; a
# governance notice or buyback announcement contains almost none.
FINANCIAL_TERMS = (
    "revenue", "net income", "net loss", "per share", "gross margin",
    "gross profit", "operating income", "operating expenses", "ebitda",
    "cash flow", "total assets", "unaudited", "diluted", "income statement",
    "balance sheet",
)

PERIOD_RE = re.compile(
    r"\b(first|second|third|fourth)[- ]quarter\b|\bq[1-4]\b|"
    r"\b(three|six|nine|twelve) months ended\b|\bquarter ended\b|"
    r"\byear ended\b|\bfull[- ]year\b|\bhalf[- ]year\b", re.IGNORECASE)

# See the module docstring: 5, not 7, because Honda's quarterlies score 5-6.
FILING_SCORE_MIN = 5

# R1.htm, R12.htm and friends are XBRL viewer render fragments, not filed
# documents. They contain financial vocabulary and would inflate scores.
_XBRL_RENDER_RE = re.compile(r"^R\d+\.htm$", re.IGNORECASE)


class FetchError(Exception):
    """Network failure, as distinct from a document that scored low.

    This distinction is not cosmetic. During testing Vale scored 1 on one run
    and 12 on the next for the identical filing -- a transient failure had
    been silently swallowed and read as "not earnings". Without a separate
    error path a dropped connection is indistinguishable from a routine
    filing, and a missed earnings report looks exactly like normal operation.
    """


def _decompress(body, encoding):
    """urllib does NOT do this for you.

    requests transparently decodes Content-Encoding; urllib hands back the
    raw bytes. This module asks for gzip -- SEC submission files are hundreds
    of KB and gzip is the difference between polite polling and hammering a
    government API -- so it has to undo it itself.

    It did not, and the failure was perfectly disguised: gzip bytes decoded
    with errors="replace" produce a string, so nothing raised until
    json.loads reported "Expecting value: line 1 column 1", which reads like
    the SEC returned an error page rather than like a client bug.

    The magic-number check is deliberate belt-and-braces. A proxy that
    decompresses the body while leaving the header in place would otherwise
    send this straight back into the same failure.
    """
    encoding = (encoding or "").lower()
    if body[:2] == b"\x1f\x8b":
        return gzip.decompress(body)
    if "deflate" in encoding:
        try:
            return zlib.decompress(body)
        except zlib.error:
            return zlib.decompress(body, -zlib.MAX_WBITS)   # raw deflate
    return body


def _get(url, timeout=15, etag=None):
    """Returns (status, body_bytes, etag). Raises FetchError on failure.

    304 comes back with an empty body: the caller already has current data.
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "gzip, deflate",
    })
    if etag:
        req.add_header("If-None-Match", etag)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = _decompress(resp.read(), resp.headers.get("Content-Encoding"))
            return resp.status, body, resp.headers.get("ETag")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, b"", etag
        raise FetchError(f"{url} -> HTTP {e.code}") from e
    except FetchError:
        raise
    except Exception as e:
        raise FetchError(f"{url} -> {type(e).__name__}: {e}") from e


def _get_json(url, timeout=15, etag=None):
    status, body, new_etag = _get(url, timeout=timeout, etag=etag)
    if status == 304:
        return None, etag
    try:
        return json.loads(body.decode("utf-8", "replace")), new_etag
    except ValueError as e:
        # Include what actually came back. "Expecting value: line 1 column 1"
        # on its own is indistinguishable between an HTML error page, an
        # empty body, and bytes we failed to decompress -- which cost a whole
        # debugging cycle.
        raise FetchError(f"{url} -> malformed JSON: {e}; "
                         f"first bytes {body[:32]!r}") from e


# -- Ticker -> CIK ---------------------------------------------------------

def load_cik_map():
    try:
        with open(CIK_MAP_FILE) as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        print(f"cik_map.json unreadable ({e}); starting empty.")
        return {}


def resolve_cik(ticker: str, cik_map: dict):
    """Ten-digit zero-padded CIK, or None.

    The bundled map covers the tickers in use. Anything else triggers one
    lookup against the SEC's full list, which is ~800KB and changes slowly --
    far too heavy to fetch on a polling loop, which is why it is cached to
    disk rather than fetched per run.
    """
    ticker = ticker.upper()
    if ticker in cik_map:
        return cik_map[ticker]

    print(f"[{ticker}] not in cik_map.json; fetching the SEC's full list.")
    try:
        data, _ = _get_json(TICKER_MAP_URL, timeout=30)
    except FetchError as e:
        print(f"[{ticker}] CIK lookup failed: {e}")
        return None
    if not data:
        return None

    for entry in data.values():
        sym = str(entry.get("ticker", "")).upper()
        if sym:
            cik_map[sym] = str(entry.get("cik_str", "")).zfill(10)
    try:
        with open(CIK_MAP_FILE, "w") as f:
            json.dump(cik_map, f, indent=2, sort_keys=True)
            f.write("\n")
    except OSError as e:
        print(f"Could not persist cik_map.json: {e}")
    return cik_map.get(ticker)


# -- Filing discovery ------------------------------------------------------

def recent_filings(cik: str, etag=None):
    """Returns (filings, etag). filings is None when nothing changed (304).

    Conditional requests matter here: at one poll every 15 seconds, an
    unchanged 304 costs a fraction of a 100KB body, and it is the difference
    between polite polling and hammering a government API.
    """
    data, new_etag = _get_json(SUBMISSIONS_URL.format(cik=cik), etag=etag)
    if data is None:
        return None, new_etag

    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", []) or []
    out = []
    for i, form in enumerate(forms):
        out.append({
            "form": form,
            "accession": (recent.get("accessionNumber") or [""] * len(forms))[i],
            "filed": (recent.get("filingDate") or [""] * len(forms))[i],
            "accepted": (recent.get("acceptanceDateTime") or [""] * len(forms))[i],
            "items": (recent.get("items") or [""] * len(forms))[i] or "",
            "doc": (recent.get("primaryDocument") or [""] * len(forms))[i] or "",
        })
    return out, new_etag


def is_domestic_earnings(filing: dict) -> bool:
    """8-K carrying item 2.02 -- the SEC's own label for a results release.

    The item list is split rather than substring-matched. A plain `in` test
    means "2.02" also matches "12.02"; no such item exists today, but a code
    added later would silently start firing false alerts, and a false
    earnings alert is expensive to trust once and lose confidence in.
    """
    if filing.get("form") != "8-K":
        return False
    codes = {c.strip() for c in (filing.get("items") or "").split(",")}
    return EARNINGS_ITEM in codes


def score_filing(cik: str, accession: str, max_docs: int = 3):
    """Count distinct financial terms in a filing's exhibits.

    Returns (score, has_period, text). Raises FetchError rather than
    returning zero when the network fails -- see FetchError.

    Every exhibit is considered, not just the largest. Nu Holdings filed four
    6-Ks on one day and the biggest HTML file in one of them was a 9.5MB
    document containing 1,304 characters of text; the press release was
    elsewhere in the same filing.
    """
    acc_nodash = accession.replace("-", "")
    base = ARCHIVES_URL.format(cik=str(int(cik)), accession=acc_nodash)

    index, _ = _get_json(f"{base}/index.json")
    if not index:
        raise FetchError(f"{base}/index.json returned nothing")

    docs = [
        item for item in (index.get("directory", {}).get("item") or [])
        if str(item.get("name", "")).lower().endswith(".htm")
        and not _XBRL_RENDER_RE.match(str(item.get("name", "")))
    ]
    # Largest first: the press release is usually the biggest real document,
    # so the answer is normally found on the first fetch.
    docs.sort(key=lambda d: int(d.get("size") or 0), reverse=True)

    best_score, best_period, best_text = 0, False, ""
    for doc in docs[:max_docs]:
        _, body, _ = _get(f"{base}/{doc['name']}", timeout=20)
        text = strip_html(body.decode("utf-8", "replace"))
        lowered = text.lower()
        score = sum(1 for term in FINANCIAL_TERMS if term in lowered)
        if score > best_score:
            best_score, best_period, best_text = score, bool(PERIOD_RE.search(text)), text
    return best_score, best_period, best_text


# Forms only a FOREIGN PRIVATE ISSUER files. This is the classification, and
# it is definitional rather than heuristic: Form 6-K exists solely for FPIs
# under Rule 13a-16, and 20-F/40-F are their annual reports. A company filing
# any of them IS one, by the SEC's own definition.
#
# Why classify at all: domestic issuers furnish results on an 8-K item 2.02
# essentially simultaneously with the press release, so EDGAR is first or
# joint-first and there is nothing to gain from watching headlines. FPIs have
# no equivalent obligation. XPeng announced Q2 2026 on the morning of
# 2026-08-24 and by mid-morning the only 6-K on EDGAR was an unrelated
# transaction notice -- the results filing had not arrived.
#
# Better than a hand-maintained list of "foreign tickers" because it cannot
# go stale: it is read from the same submissions payload the watcher already
# fetches, so a re-domiciled company reclassifies itself.
FOREIGN_FORMS = ("6-K", "20-F", "40-F")


# Forms that can never be a results release but routinely dominate a filing
# list. Insider transactions and ownership disclosures are filed constantly by
# recently-IPO'd companies with vesting equity.
#
# This exists because of a measured failure, not a hunch. On 2026-08-27 a
# replay of RBRK reported "no recent filing classifies as earnings" while its
# results 8-K sat plainly on EDGAR: the scan looked at the 15 newest filings
# and RBRK's newest 8-K was at index 28, behind 28 consecutive Form 4s, Form
# 144s and 13Gs.
#
# The lesson generalises past this one list: bound a search by the thing you
# are looking for, not by rows of whatever happens to be in front of it.
NOISE_FORMS = frozenset({
    "3", "4", "5", "144",
    "SC 13G", "SC 13G/A", "SC 13D", "SC 13D/A",
    "SCHEDULE 13G", "SCHEDULE 13G/A", "SCHEDULE 13D", "SCHEDULE 13D/A",
})

# Forms that can carry a results release. 8-K for domestic filers, 6-K for
# foreign private issuers.
CANDIDATE_FORMS = frozenset({"8-K", "6-K"})


def earnings_candidates(filings, limit: int = 15):
    """The recent filings that could be a results release, newest first.

    Filtering by form before taking `limit` is the whole point: a count over
    raw rows measures insider-filing volume, not filing history.
    """
    out = []
    for filing in filings or []:
        if str(filing.get("form", "")).upper() in CANDIDATE_FORMS:
            out.append(filing)
            if len(out) >= limit:
                break
    return out


def is_foreign_issuer(filings, lookback: int = 40) -> bool:
    """True if this company files as a foreign private issuer.

    `lookback` bounds it to recent history so a company that converted to
    domestic filing status years ago is not classified on ancient forms. It
    counts only substantive forms, because otherwise a run of insider filings
    consumes the whole window and a foreign issuer reads as domestic --
    which would silently disable the wire-headline early signal for exactly
    the companies it was built for.
    """
    seen = 0
    for filing in filings or []:
        form = str(filing.get("form", "")).upper()
        if form in NOISE_FORMS:
            continue
        if form in FOREIGN_FORMS:
            return True
        seen += 1
        if seen >= lookback:
            break
    return False


def is_foreign_earnings(score: int, has_period: bool) -> bool:
    """A 6-K is results if it reads like financial statements AND names a
    period. Both halves are needed: JD filed a 23KB 6-K that mentioned a
    quarter but contained no financial terms at all."""
    return score >= FILING_SCORE_MIN and has_period


def strip_html(raw: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", raw, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">")
                .replace("&#39;", "'").replace("&quot;", '"'))
    return re.sub(r"\s+", " ", text).strip()


def filing_url(cik: str, accession: str, doc: str = "") -> str:
    base = ARCHIVES_URL.format(cik=str(int(cik)),
                               accession=accession.replace("-", ""))
    return f"{base}/{doc}" if doc else f"{base}/"
