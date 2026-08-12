"""TEMPORARY diagnostic: how much do Yahoo and Google News actually overlap?

Answers a specific question: if we dropped the Yahoo Finance feed and relied
only on Google News RSS, would we lose any alerts we currently get?

Raw overlap is the wrong metric on its own -- what matters is whether there
are MATERIAL headlines (ones that pass is_material and would therefore
actually be sent to Telegram) that only one source carries.

Delete this file and its workflow once the question is settled.
"""

import difflib

import monitor
from config import TICKERS, WATCHLIST
from state_utils import load_state


def norm(title):
    return " ".join(str(title or "").lower().split())


def matches(title, pool, threshold=0.82):
    """Same fuzzy comparison monitor.py uses for cross-source dedup."""
    t = norm(title)
    for other in pool:
        if difflib.SequenceMatcher(None, t, norm(other)).ratio() >= threshold:
            return other
    return None


def yahoo_titles(ticker):
    out = []
    try:
        for art in (monitor.yf.Ticker(ticker).news or []):
            content = art.get("content", art)
            title = content.get("title")
            if title:
                out.append(title)
    except Exception as e:
        print(f"  [!] Yahoo fetch failed: {type(e).__name__}: {e}")
    return out


def google_titles(ticker, state):
    import xml.etree.ElementTree as ET
    from urllib.parse import quote
    out = []
    try:
        query = quote(monitor._news_query(ticker, state))
        url = (
            "https://news.google.com/rss/search?q=" + query
            + "&hl=en-US&gl=US&ceid=US:en"
        )
        resp = monitor.get_with_retry(
            url, headers=monitor.GOOGLE_NEWS_HEADERS, timeout=15, label=f"{ticker} g"
        )
        if resp is None:
            print("  [!] Google fetch failed")
            return out
        root = ET.fromstring(resp.content)
        for item in root.findall("./channel/item"):
            title = item.findtext("title")
            if title:
                out.append(title)
    except Exception as e:
        print(f"  [!] Google fetch failed: {type(e).__name__}: {e}")
    return out


def main():
    state = load_state()
    monitored = sorted(set(TICKERS) | set(WATCHLIST))

    tot_y = tot_g = tot_shared = 0
    material_only_y = []
    material_only_g = []

    for ticker in monitored:
        y = yahoo_titles(ticker)
        g = google_titles(ticker, state)

        shared = [t for t in y if matches(t, g)]
        only_y = [t for t in y if not matches(t, g)]
        only_g = [t for t in g if not matches(t, y)]

        tot_y += len(y)
        tot_g += len(g)
        tot_shared += len(shared)

        print(f"\n=== {ticker} ===")
        print(f"  yahoo={len(y)}  google={len(g)}  overlapping={len(shared)}")
        print(f"  only-yahoo={len(only_y)}  only-google={len(only_g)}")

        for t in only_y:
            flag = "MATERIAL" if monitor.is_material(t) else "        "
            print(f"    [Y-only][{flag}] {t[:110]}")
            if monitor.is_material(t):
                material_only_y.append((ticker, t))
        for t in only_g:
            flag = "MATERIAL" if monitor.is_material(t) else "        "
            print(f"    [G-only][{flag}] {t[:110]}")
            if monitor.is_material(t):
                material_only_g.append((ticker, t))

    print("\n" + "=" * 60)
    print("TOTALS")
    print(f"  yahoo articles:  {tot_y}")
    print(f"  google articles: {tot_g}")
    print(f"  overlapping:     {tot_shared}")
    if tot_y:
        print(f"  share of Yahoo articles also on Google: {100.0 * tot_shared / tot_y:.1f}%")
    print()
    print(f"  MATERIAL headlines ONLY on Yahoo:  {len(material_only_y)}")
    for ticker, t in material_only_y:
        print(f"    {ticker}: {t[:110]}")
    print(f"  MATERIAL headlines ONLY on Google: {len(material_only_g)}")
    for ticker, t in material_only_g:
        print(f"    {ticker}: {t[:110]}")
    print()
    print("  -> Dropping Yahoo is safe only if 'MATERIAL only on Yahoo' is ~0.")
    print("=" * 60)


if __name__ == "__main__":
    main()
