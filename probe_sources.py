"""TEMPORARY probe: which earnings sources are reachable from the runner?

The EDGAR attempt failed because sec.gov returns 403 to GitHub Actions IP
ranges -- discovered only after building the whole module. This checks
reachability FIRST so the IR-site / wire-feed design is built against
sources that actually work from CI.

Delete this file and its workflow once the design is settled.
"""

import requests

UA = {"User-Agent": "Mozilla/5.0 (compatible; stock-alert-agent/1.0)"}

CANDIDATES = [
    ("cerebras IR news page",
     "https://investors.cerebras.ai/news-events/news-releases"),
    ("cerebras IR rss guess 1",
     "https://investors.cerebras.ai/rss/news-releases.xml"),
    ("cerebras IR rss guess 2",
     "https://investors.cerebras.ai/feed"),
    ("globenewswire newsroom rss",
     "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire-News-Room"),
    ("globenewswire search",
     "https://www.globenewswire.com/en/search/keyword/Cerebras"),
    ("businesswire home",
     "https://www.businesswire.com/portal/site/home/news/"),
    ("prnewswire news releases",
     "https://www.prnewswire.com/news-releases/"),
    ("google news rss (baseline)",
     "https://news.google.com/rss/search?q=%22Cerebras+Systems%22&hl=en-US&gl=US&ceid=US:en"),
    ("sec.gov (expected 403)",
     "https://www.sec.gov/files/company_tickers.json"),
]

NEEDLES = ("second quarter 2026", "second-quarter 2026", "q2 2026", "cerebras")


def main():
    print(f"{'source':<32} {'status':<8} {'bytes':<9} needles")
    print("-" * 78)
    for label, url in CANDIDATES:
        try:
            resp = requests.get(url, headers=UA, timeout=25)
            body = resp.text or ""
            low = body.lower()
            hits = [n for n in NEEDLES if n in low]
            print(f"{label:<32} {resp.status_code:<8} {len(body):<9} {hits}")
        except Exception as e:
            print(f"{label:<32} {'ERR':<8} {'-':<9} {type(e).__name__}")


if __name__ == "__main__":
    main()
