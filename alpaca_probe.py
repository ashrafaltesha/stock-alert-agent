"""Does Alpaca/Benzinga actually cover YOUR stocks? Run once, then delete.

This answers the only question that decides whether Alpaca replaces the
Google News collector: coverage. Alpaca's own docs say the feed carries
"an average of 130+ news articles per day" across all US stocks AND crypto,
which is a curated wire rather than a comprehensive one. Benzinga skews US
large- and mid-cap; this portfolio does not. GENI, KEEL, UAVS, BAK and CENX
are exactly the names such a feed is most likely to skip.

Trading a feed that is LATE for one that is SILENT would be a bad deal, and
silence is the failure mode this project has repeatedly proven worst at
noticing. So: measure before rewriting anything.

For each ticker this prints, over the last 7 days:

    alpaca   how many articles, and how fresh the newest one is
    google   the same, from the feed the bot reads today

Read the ALPACA column first. A ticker with 0 there is one that Alpaca
cannot alert you about, ever, no matter how good the plumbing is.

Usage -- the key never appears in this file or in the repo:

    export APCA_API_KEY_ID='...'
    export APCA_API_SECRET_KEY='...'
    python3 alpaca_probe.py

Standard library only, so there is nothing to install.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

NEWS_URL = "https://data.alpaca.markets/v1beta1/news"
GOOGLE_URL = "https://news.google.com/rss/search"
DAYS = 7
UA = "Mozilla/5.0 (compatible; personal-stock-alerts/1.0)"


def _tickers():
    """The real watchlist, read from the repo rather than retyped."""
    here = os.path.dirname(os.path.abspath(__file__))
    out = []
    for name in ("tickers.json", "watchlist.json"):
        try:
            with open(os.path.join(here, name)) as f:
                out += json.load(f)
        except (OSError, ValueError):
            pass
    return sorted(set(out))


def _company_names():
    """Reuse the names the bot already resolved, so the Google comparison
    uses the SAME query the bot sends rather than a flattering one."""
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        with open(os.path.join(here, "state.json")) as f:
            state = json.load(f)
    except (OSError, ValueError):
        return {}
    return {k.split("::", 1)[1]: v for k, v in state.items()
            if k.startswith("company_name::")}


def _get(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def alpaca_news(symbols, key, secret, start):
    """{symbol: [(age_minutes, headline), ...]} over the window."""
    found = {s: [] for s in symbols}
    page = None
    while True:
        params = {"symbols": ",".join(symbols), "start": start,
                  "limit": "50", "sort": "desc"}
        if page:
            params["page_token"] = page
        url = f"{NEWS_URL}?{urllib.parse.urlencode(params)}"
        try:
            body = _get(url, headers={
                "APCA-API-KEY-ID": key,
                "APCA-API-SECRET-KEY": secret,
                "Accept": "application/json",
                "User-Agent": UA,
            })
        except urllib.error.HTTPError as e:
            detail = e.read()[:200].decode("utf-8", "replace")
            print(f"\nAlpaca returned HTTP {e.code}: {detail}")
            if e.code in (401, 403):
                print("That is an auth failure -- check the two environment "
                      "variables are set and belong to the same key pair.")
            sys.exit(1)
        except Exception as e:
            print(f"\nCould not reach Alpaca: {type(e).__name__}: {e}")
            sys.exit(1)

        data = json.loads(body)
        now = datetime.now(timezone.utc)
        for art in data.get("news", []) or []:
            try:
                created = datetime.fromisoformat(
                    art["created_at"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            age = (now - created).total_seconds() / 60
            for sym in art.get("symbols", []):
                if sym in found:
                    found[sym].append((age, art.get("headline", "")))
        page = data.get("next_page_token")
        if not page:
            return found


def google_news(ticker, names):
    """Same query the bot sends today, so the comparison is fair."""
    query = names.get(ticker) or (ticker + " stock")
    quoted = urllib.parse.quote('"' + query + '"')
    url = GOOGLE_URL + "?q=" + quoted + "&hl=en-US&gl=US&ceid=US:en"
    try:
        body = _get(url, timeout=20)
        items = ET.fromstring(body).findall("./channel/item")
    except Exception as e:
        return None, f"{type(e).__name__}"

    now = datetime.now(timezone.utc)
    ages = []
    for item in items:
        raw = item.findtext("pubDate")
        try:
            published = parsedate_to_datetime(raw)
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            ages.append((now - published).total_seconds() / 60)
        except Exception:
            continue
    return sorted(ages), None


def main():
    key = os.environ.get("APCA_API_KEY_ID")
    secret = os.environ.get("APCA_API_SECRET_KEY")
    if not key or not secret:
        print(__doc__)
        print("Set APCA_API_KEY_ID and APCA_API_SECRET_KEY first.")
        sys.exit(1)

    symbols = _tickers()
    if not symbols:
        print("No tickers found -- run this from inside the repo.")
        sys.exit(1)
    names = _company_names()
    start = (datetime.now(timezone.utc) - timedelta(days=DAYS)).strftime("%Y-%m-%d")

    print(f"Comparing {len(symbols)} tickers over the last {DAYS} days:")
    print(f"  {', '.join(symbols)}\n")

    alpaca = alpaca_news(symbols, key, secret, start)

    print(f"{'':6} {'ALPACA':>18}   {'GOOGLE (what we use now)':>28}")
    print(f"{'':6} {'arts':>5} {'newest':>11}   {'arts':>5} {'newest':>11} {'<6h':>5} {'<24h':>5}")
    print("-" * 72)

    silent, covered = [], []
    for sym in symbols:
        arts = sorted(alpaca.get(sym, []))
        a_n = len(arts)
        a_new = f"{arts[0][0]/60:.1f}h" if arts else "--"
        g_ages, err = google_news(sym, names)
        if g_ages is None:
            g_n, g_new, g6, g24 = "err", err[:9], "-", "-"
        else:
            g_n = len(g_ages)
            g_new = f"{g_ages[0]/60:.1f}h" if g_ages else "--"
            g6 = sum(1 for x in g_ages if x <= 360)
            g24 = sum(1 for x in g_ages if x <= 1440)
        print(f"{sym:6} {a_n:>5} {a_new:>11}   {g_n:>5} {g_new:>11} {g6:>5} {g24:>5}")
        (covered if a_n else silent).append(sym)

    print("\n--- verdict ---")
    print(f"Alpaca carries news for {len(covered)}/{len(symbols)}: "
          f"{', '.join(covered) or 'none'}")
    if silent:
        print(f"Alpaca has NOTHING for: {', '.join(silent)}")
        print("Those tickers would go dark if Alpaca replaced Google outright.")
        print("Keep Google as a slow backstop for them, or run both.")
    else:
        print("Every ticker is covered -- Alpaca can replace the Google "
              "collector rather than sit alongside it.")

    print("\nSample of what Alpaca actually returned:")
    for sym in covered[:4]:
        age, head = sorted(alpaca[sym])[0]
        print(f"  [{sym}] {age/60:5.1f}h  {head[:88]}")


if __name__ == "__main__":
    main()
