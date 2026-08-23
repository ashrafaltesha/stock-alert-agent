"""TEMPORARY probe: does data.sec.gov answer a GitHub Actions runner?

This is the only open question left about a source that would otherwise solve
earnings detection outright.

Why this matters
----------------
Every earnings approach so far has failed for the same underlying reason: the
runner's IP is a datacenter address and the site declines to serve it. That
was true of www.sec.gov (403), efts.sec.gov (403), GlobeNewswire and Business
Wire (timeout), and it is why Genius Sports and AppLovin return nothing --
they serve a reCAPTCHA or an empty shell to anything automated.

But two SEC hosts were tested and a THIRD was never tried. data.sec.gov is
the SEC's structured JSON API, separate infrastructure from the website, and
explicitly built for programmatic access rather than browsing.

Verified from a browser, it returns for every ticker checked:

    AMAT  8-K  accepted 2026-08-13T20:03:36Z  items 2.02,9.01
    GENI  6-K  accepted 2026-08-06T11:00:05Z
    XPEV  6-K  accepted 2026-08-04T12:50:55Z

Three things make that better than anything built so far:

1. Item 2.02 is "Results of Operations and Financial Condition". The SEC
   labels the earnings release itself. No keyword matching, no date-proximity
   parsing, no judging whether a headline sounds like earnings -- all of
   which this project has already got wrong at least once each.
2. acceptanceDateTime is precise to the second. Applied Materials filed 3
   minutes 36 seconds after the closing bell.
3. Coverage is universal, because filing is a legal obligation. It works for
   Genius Sports and AppLovin, which block scrapers but still file.

What this probe answers
-----------------------
1. Does data.sec.gov return 200 from the runner, or 403 like its siblings?
2. Does the SEC's declared-User-Agent requirement change the answer?
3. Are the ticker->CIK mapping files reachable, or must the map be cached in
   the repo?
4. For each holding: what does its filing history actually look like, and is
   "new 8-K with item 2.02" (or "new 6-K" for foreign issuers) a clean signal?

Sends nothing, writes nothing. Delete once the answer is recorded.
"""

import json
import sys
import time

import requests

# The SEC asks automated clients to identify themselves with a contact
# address. This is their documented format. It did NOT stop www.sec.gov
# returning 403, but that may have been IP-based rather than UA-based, so it
# is tested here both ways to tell the two apart.
DECLARED_UA = "ashrafaltesha personal-stock-alerts altesha@outlook.com"
GENERIC_UA = "Mozilla/5.0 (compatible; stock-alert-agent/1.0)"

SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"

# Resolved from www.sec.gov/files/company_tickers.json in a browser. Cached
# here because that file lives on the host that 403s the runner -- if the
# probe shows it stays blocked, this map ships with the repo permanently.
CIKS = {
    "CBRS": "0002021728",
    "GENI": "0001834489",
    "EVH":  "0001628908",
    "XPEV": "0001810997",
    "QURE": "0001590560",
    "UAVS": "0000008504",
    "WOLF": "0000895419",
    "FOUR": "0001794669",
    "APP":  "0001751008",
    # Two that reported on 2026-08-13, as a freshness check.
    "AMAT": "0000006951",
    "TPR":  "0001116132",
}

# Forms that carry results. 8-K is domestic; 6-K is the foreign-issuer
# equivalent and carries NO item codes, so it needs a different rule.
EARNINGS_ITEM = "2.02"


def get(url, ua, label, timeout=20):
    try:
        r = requests.get(url, headers={"User-Agent": ua,
                                       "Accept": "application/json"},
                         timeout=timeout)
        return r
    except Exception as e:
        print(f"    {label}: ERR {type(e).__name__}: {e}")
        return None


def main():
    print("=" * 72)
    print("1. REACHABILITY -- the question this probe exists to answer")
    print("=" * 72)

    url = SUBMISSIONS.format(cik=CIKS["AMAT"])
    results = {}
    for name, ua in (("declared UA", DECLARED_UA), ("generic UA", GENERIC_UA)):
        r = get(url, ua, name)
        code = r.status_code if r is not None else "ERR"
        results[name] = code
        print(f"  {name:12} -> {code}")
        if r is not None and r.status_code != 200:
            print(f"    body: {r.text[:200]!r}")

    if 200 not in results.values():
        print()
        print("  >>> data.sec.gov is blocked from this runner too.")
        print("  >>> The source is right but the location is wrong. Next step")
        print("  >>> is moving the poller off GitHub's IP range -- Cloudflare")
        print("  >>> Workers (free, cron triggers) or a machine at home.")
        return 0

    ua = DECLARED_UA if results.get("declared UA") == 200 else GENERIC_UA
    print(f"\n  >>> REACHABLE. Using: {ua.split()[0]}...")

    print()
    print("=" * 72)
    print("2. TICKER -> CIK MAPPING -- can it be fetched, or must it be cached?")
    print("=" * 72)
    for host in ("https://www.sec.gov/files/company_tickers.json",
                 "https://data.sec.gov/files/company_tickers.json"):
        r = get(host, ua, host)
        code = r.status_code if r is not None else "ERR"
        print(f"  {host} -> {code}")
        if r is not None and r.status_code == 200:
            print(f"    {len(r.content)} bytes -- mapping can be refreshed at runtime.")
    print("  (If both fail, the CIK map above ships with the repo. It changes")
    print("   slowly, so a stale entry is a minor problem, not a broken one.)")

    print()
    print("=" * 72)
    print("3. PER-TICKER FILING HISTORY -- is the signal clean?")
    print("=" * 72)

    for ticker, cik in CIKS.items():
        # The SEC asks for no more than 10 requests/second. This is nowhere
        # near that, but being a well-behaved client is how access stays.
        time.sleep(0.2)
        r = get(SUBMISSIONS.format(cik=cik), ua, ticker)
        if r is None or r.status_code != 200:
            print(f"\n  {ticker}: HTTP {r.status_code if r else 'ERR'}")
            continue

        try:
            data = r.json()
        except ValueError:
            print(f"\n  {ticker}: non-JSON response")
            continue

        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accepted = recent.get("acceptanceDateTime", [])
        items = recent.get("items", [])

        print(f"\n  {ticker} -- {data.get('name','?')}")

        # How often does the earnings-bearing form appear? A form that shows
        # up constantly is a noisy trigger; one that appears quarterly is a
        # clean one.
        eightk = [i for i, f in enumerate(forms) if f == "8-K"]
        sixk = [i for i, f in enumerate(forms) if f == "6-K"]
        results_8k = [i for i in eightk
                      if EARNINGS_ITEM in (items[i] if i < len(items) else "")]

        print(f"    last 1000 filings: {len(eightk)} 8-K, {len(sixk)} 6-K, "
              f"{len(results_8k)} of the 8-Ks carry item {EARNINGS_ITEM}")

        for i in (results_8k or sixk)[:3]:
            label = "8-K item 2.02" if i in results_8k else "6-K"
            print(f"      {label:14} filed {dates[i]}  accepted {accepted[i]}")

        if not results_8k and not sixk:
            print("      >>> neither form present -- needs a look")
        elif not results_8k:
            print("      >>> foreign issuer: 6-K has no item codes, so the")
            print("      >>> rule is 'a new 6-K appeared', which is noisier.")

    print()
    print("=" * 72)
    print("VERDICT")
    print("=" * 72)
    print("  If section 1 says REACHABLE, this replaces the IR-page scraping")
    print("  entirely: universal coverage, second-precision timestamps, and an")
    print("  official earnings marker instead of guessing from headlines.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
