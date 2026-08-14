"""TEMPORARY probe: can FMP's press-release feed replace per-company IR feeds?

Background
----------
Detection has to answer "the earnings release just landed" within a minute or
two. Three sources have been measured from the GitHub Actions runner so far:

    sec.gov / efts.sec.gov          403  (SEC blocks datacenter IPs)
    GlobeNewswire / Business Wire   timeout
    IR RSS feeds                    200, but only 2 of 9 tickers have one

The IR-feed route works and is free, but hunting feed URLs stalled at 2/9:
the rest 404 on every guessed path or fail DNS entirely.

FMP publishes its refresh cadence, and it rules out the obvious endpoints:
its 8-K feed refreshes hourly and its Earnings Report data every 1-2 hours --
both far too slow, and the 1-2h lag is exactly what made Finnhub miss the
Cerebras release. But its *press-release* feed refreshes every 5 minutes,
covers every company from one endpoint, and is served from FMP's own
infrastructure rather than linking back to blocked sec.gov.

If that holds up, it replaces the whole FEED_URLS map.

What this probe decides
-----------------------
1. Does a FREE-tier key reach /stable/news/press-releases at all? (Paid-only
   endpoints return 402/403 rather than an empty list, so this is
   unambiguous.)
2. What is the response shape -- specifically, is there a body/text field, or
   only a headline? Summaries are worthless without one.
3. Does `symbols=` accept a comma-separated list? If so, one call covers all
   nine tickers and the free tier's 250 calls/day is comfortable rather than
   tight.
4. Coverage: does every ticker return entries, including the foreign issuers?
5. Does the existing is_results_entry() classifier fire correctly on real FMP
   entries -- catching genuine releases without flagging scheduling notices?
6. How fresh is the newest entry, as a sanity check on the claimed 5-minute
   cycle?

Sends nothing, writes nothing, and prints no key material. Delete with its
workflow once the source design is settled.
"""

import json
import os
from datetime import datetime, timezone

import requests

import ir_feeds

API_KEY = os.environ.get("FMP_API_KEY", "")
BASE = "https://financialmodelingprep.com/stable/news/press-releases"
TIMEOUT = 20

TICKERS = ["CBRS", "GENI", "EVH", "XPEV", "QURE", "UAVS", "WOLF", "FOUR", "APP"]


def call(params, label):
    """One request. Returns (status, parsed_or_none, note). Never logs the key."""
    q = dict(params)
    q["apikey"] = API_KEY
    try:
        resp = requests.get(BASE, params=q, timeout=TIMEOUT)
    except Exception as e:
        print(f"  {label}: ERR {type(e).__name__}")
        return None, None, "error"

    note = ""
    body = None
    try:
        body = resp.json()
    except ValueError:
        note = f"non-JSON body: {resp.text[:200]!r}"

    print(f"  {label}: HTTP {resp.status_code}"
          + (f" | {note}" if note else "")
          + (f" | {len(body)} entries" if isinstance(body, list) else ""))

    # FMP signals plan restrictions in the body, not always the status code.
    if isinstance(body, dict):
        print(f"    response object: {json.dumps(body)[:300]}")

    return resp.status_code, body, note


def parse_dt(raw):
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def main():
    if not API_KEY:
        print("FMP_API_KEY is not set -- nothing to probe.")
        return

    print("=" * 70)
    print("1. ACCESS + SHAPE  (single ticker, CBRS)")
    print("=" * 70)
    status, body, _ = call({"symbols": "CBRS", "limit": 20}, "CBRS")

    if status != 200 or not isinstance(body, list):
        print("\n  >>> Free tier does NOT reach this endpoint. Stop here;")
        print("  >>> the press-release route needs a paid plan.")
        return
    if not body:
        print("\n  >>> Reachable but empty for CBRS. Check symbol coverage below.")

    if body:
        sample = body[0]
        print(f"\n  fields: {sorted(sample.keys())}")
        for key, value in sample.items():
            shown = value if isinstance(value, (int, float)) else str(value)
            print(f"    {key:16} ({len(str(value)):>5} chars) {shown[:110]!r}"
                  if not isinstance(value, (int, float))
                  else f"    {key:16} {value}")

        # The decisive question: is there a usable body, or only a headline?
        text_fields = [k for k, v in sample.items()
                       if isinstance(v, str) and len(v) > 200]
        print(f"\n  fields with >200 chars (candidate release bodies): {text_fields}")
        if not text_fields:
            print("  >>> WARNING: headline-only. Summaries would carry no numbers.")

        newest = parse_dt(str(sample.get("publishedDate") or sample.get("date") or ""))
        if newest:
            age = (datetime.now(timezone.utc) - newest).total_seconds() / 3600
            print(f"  newest entry age: {age:.1f}h "
                  f"(sanity check only -- says nothing about cycle time "
                  f"unless CBRS published recently)")

    print()
    print("=" * 70)
    print("2. BATCHING  (does symbols= accept a comma list?)")
    print("=" * 70)
    status, batch, _ = call(
        {"symbols": ",".join(TICKERS), "limit": 100}, "all 9 in one call"
    )
    if status == 200 and isinstance(batch, list) and batch:
        seen = sorted({e.get("symbol") for e in batch if e.get("symbol")})
        print(f"  distinct symbols returned: {seen}")
        if len(seen) > 1:
            print("  >>> Batching WORKS. One call per poll covers every armed watch,")
            print("  >>> so 250 calls/day is ample even polling every 5 minutes.")
        else:
            print("  >>> Only one symbol came back -- treat as unbatched,")
            print("  >>> i.e. one call per ticker per poll.")
    else:
        print("  >>> Batching not supported; one call per ticker.")

    print()
    print("=" * 70)
    print("3. COVERAGE  (per ticker)")
    print("=" * 70)
    for ticker in TICKERS:
        status, entries, _ = call({"symbols": ticker, "limit": 10}, ticker)
        if status == 200 and isinstance(entries, list) and entries:
            newest = entries[0]
            when = newest.get("publishedDate") or newest.get("date") or "?"
            print(f"    latest: [{when}] {str(newest.get('title'))[:90]!r}")
        else:
            print("    >>> NO ENTRIES")

    print()
    print("=" * 70)
    print("4. CLASSIFIER  (does is_results_entry fire on real FMP entries?)")
    print("=" * 70)
    print("Reusing the exact classifier the bot runs, so a pass here means the")
    print("existing logic transfers unchanged; a miss is a real bug to fix.\n")
    status, entries, _ = call({"symbols": "CBRS", "limit": 50}, "CBRS history")
    if status == 200 and isinstance(entries, list):
        hits = 0
        for entry in entries:
            title = str(entry.get("title") or "")
            text = str(entry.get("text") or entry.get("content") or "")
            verdict = ir_feeds.is_results_entry(title, text)
            if verdict:
                hits += 1
            mark = "RESULTS " if verdict else "        "
            when = str(entry.get("publishedDate") or entry.get("date") or "?")[:16]
            print(f"  {mark}[{when}] {title[:88]}")
        print(f"\n  classified as results releases: {hits} of {len(entries)}")
        print("  Expect roughly one per quarter. Zero means the classifier misses")
        print("  FMP's title style; many means it is far too loose.")


if __name__ == "__main__":
    main()
