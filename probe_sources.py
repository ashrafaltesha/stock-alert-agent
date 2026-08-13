"""TEMPORARY probe: find each ticker's investor-relations RSS feed.

Round 1 established that IR *RSS feeds* are reachable from the runner while
IR HTML pages, GlobeNewswire and Business Wire all time out, and sec.gov
403s. investors.cerebras.ai/rss/news-releases.xml returned 200 and already
contained the Q2 2026 release.

This round tests whether that same feed path resolves for the rest of the
portfolio, so ir_feeds.py can be built against verified URLs. Short timeout
because a dead host just hangs.

Delete with its workflow once the feed map is settled.
"""

import requests

UA = {"User-Agent": "Mozilla/5.0 (compatible; stock-alert-agent/1.0)"}
TIMEOUT = 12

HOSTS = {
    "CBRS": ["investors.cerebras.ai"],
    "GENI": ["investors.geniussports.com", "ir.geniussports.com"],
    "EVH":  ["ir.evolenthealth.com", "investors.evolenthealth.com"],
    "XPEV": ["ir.xiaopeng.com", "ir.xpeng.com"],
    "QURE": ["ir.uniqure.com", "investors.uniqure.com"],
    "UAVS": ["ir.ageagle.com", "investors.ageagle.com"],
    "WOLF": ["investor.wolfspeed.com", "ir.wolfspeed.com"],
    "FOUR": ["investors.shift4.com", "ir.shift4.com"],
    "APP":  ["investors.applovin.com", "ir.applovin.com"],
}

PATHS = [
    "/rss/news-releases.xml",
    "/rss/news.xml",
]


def looks_like_feed(body):
    head = body[:400].lower()
    return "<rss" in head or "<feed" in head or "<?xml" in head


def main():
    for ticker, hosts in HOSTS.items():
        found = False
        for host in hosts:
            for path in PATHS:
                url = "https://" + host + path
                try:
                    resp = requests.get(url, headers=UA, timeout=TIMEOUT)
                    body = resp.text or ""
                    ok = resp.status_code == 200 and looks_like_feed(body)
                    print(f"{ticker} | {url} | {resp.status_code} | {len(body)}b | feed={ok}")
                    if ok:
                        found = True
                        break
                except Exception as e:
                    print(f"{ticker} | {url} | ERR | - | {type(e).__name__}")
            if found:
                break
        if not found:
            print(f"{ticker} | >>> NO FEED FOUND")
        print("")


if __name__ == "__main__":
    main()
