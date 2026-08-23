"""
Central configuration for the stock alert agent.
"""

import json
import os


def _load_tickers() -> list[str]:
    """Your held tickers live in tickers.json, not here, so they can be
    updated automatically -- text the bot "add TICKER to my list" or
    "remove TICKER from my list" and telegram_commands.py updates the file
    for you (see .github/workflows/telegram_commands.yml). You can also
    edit tickers.json by hand any time."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tickers.json")
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        print(f"Failed to load tickers.json: {e}")
        return []


TICKERS = _load_tickers()


def _load_watchlist() -> list[str]:
    """Tickers you want price/news alerts for even though you don't own
    them -- kept in watchlist.json, separate from tickers.json (which backs
    "add TICKER to my list" and gets auto-populated by "added ... shares of
    TICKER at ..."). Text the bot "add TICKER, TICKER to my watchlist" or
    "remove TICKER from my watchlist" to manage it, or "watchlist" to see
    it -- see telegram_commands.py. monitor.py polls the union of TICKERS
    and WATCHLIST for price/news alerts, so watchlist-only tickers get the
    same 5% price-move alerting as your holdings; they're NOT included in
    earnings_watch.py's per-holding earnings reminders, since those are
    specifically about things you own."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist.json")
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        print(f"Failed to load watchlist.json: {e}")
        return []


WATCHLIST = _load_watchlist()

# Alert threshold: percent move away from the PRIOR DAY'S CLOSE that
# triggers a price alert (the anchor never moves during the day). Alerts
# fire on every threshold step reached in either direction (e.g. at 5%:
# +5% from previous close alerts, and if price keeps climbing, +10%,
# +15%, etc. from that SAME close each alert once per day).
PRICE_CHANGE_THRESHOLD_PCT = 5.0

# How far back to look for "new" news articles on each run (minutes).
# Should be >= the longer of the two cron intervals (60 min, off-hours)
# with a little buffer -- the 5-min market-hours cadence works fine with
# this same window since duplicate alerts are prevented separately by the
# seen-article-id dedup, not by this window.
NEWS_LOOKBACK_MINUTES = 70

# Keyword filter for "material" news -- only headlines matching one of
# these (case-insensitive substring match) get sent as alerts. Everything
# else is still tracked for dedup but not sent. This is simple keyword
# matching, not real NLP classification, so tune this list if it's too
# noisy (too broad) or too quiet (missing real catalysts).
MATERIAL_NEWS_KEYWORDS = [
    # Analyst actions
    "upgrade", "downgrade", "initiates coverage", "resumes coverage",
    "price target", "raises target", "cuts target", "reiterates",
    "outperform", "underperform", "overweight", "underweight",
    "buy rating", "sell rating", "hold rating",
    # M&A, partnerships, deals
    "acquisition", "acquires", "acquired", "to acquire", "merger", "merges",
    "buyout", "partnership", "collaborat", "joint venture",
    "strategic alliance", "signs agreement", "signs deal",
    "definitive agreement", "licensing agreement",
    # Delivery / production numbers
    "delivery numbers", "deliveries", "delivery figures",
    "vehicle deliveries", "production numbers", "units delivered",
    # Other major catalysts
    "fda approval", "fda clearance", "clinical trial results",
    "guidance", "contract win", "contract award", "regulatory approval",
]

# -- Earnings detection (earnings_watch.py + sec_edgar.py) ------------------
#
# Detection is SEC filings, nothing else. For domestic filers an 8-K carrying
# item 2.02 ("Results of Operations and Financial Condition") IS the earnings
# release -- the SEC labels it, so nothing has to be inferred. Foreign issuers
# file 6-K, which has no item codes, so those are scored on financial content
# (see sec_edgar.FILING_SCORE_MIN).
#
# What this replaced, and why:
#   Finnhub epsActual   lagged the Cerebras release by an entire evening
#   IR page scraping    worked for ~half the holdings; GENI serves a
#                       reCAPTCHA and APP an empty shell
#   FMP press releases  HTTP 402, paid tier only
#   SEC EDGAR (www/efts) 403 to GitHub runners -- but data.sec.gov answers
#
# There are no poll windows any more. Measured across 40 foreign issuers and
# 166 domestic filings, earnings land anywhere from 06:00 to 17:23 ET; the old
# 06:00-09:00 / 16:00-18:00 pair missed Itau (09:50), Shell (10:02), Ericsson
# (10:14), HSBC (11:46), Vale (12:08) and Tesla (09:01-09:20) entirely.
#
# Calendars survive for one job only: knowing a report is SCHEDULED, which a
# filing can never tell you in advance. TWO are used -- Finnhub and Nasdaq --
# and either one listing a company arms it.
#
# Arming is the single point of failure in the whole system: no watch armed
# means the SEC detection never runs, however good it is. Free calendars miss,
# and they miss hardest on recent IPOs and foreign issuers -- which describes
# Cerebras, Genius Sports and XPeng. Wall Street Horizon sells confirmed-vs-
# estimated dates to institutions precisely because this is a hard problem.
#
# Unioning rather than intersecting is deliberate: a wasted watch costs a few
# hundred HTTP requests and expires quietly, a missed one costs the alert.

# How long a watch stays armed once created. Deliberately longer than a day:
# a company reporting pre-market at ~6am would otherwise need the command sent
# overnight. Armed the previous afternoon, both release times are covered.
ON_DEMAND_WATCH_HOURS = 24

# -- "earnings today" list (market_earnings_watch.py) -----------------------
#
# Only the calendar list survives here. The market-wide RELEASE watcher was
# removed: it still polled Finnhub's epsActual and held a runner in a
# time.sleep loop for up to three hours.

# How many top market-wide earnings reporters to list, ranked by market cap.
TOP_N_EARNINGS = 10

# How many more to list based on analyst attention, among that day's
# reporters not already in the market-cap list above.
TOP_N_ANALYST_ATTENTION = 5

# Cap on how many of the day's reporters get an analyst-coverage lookup.
# Keeps the job fast on busy days (100+ companies reporting).
ANALYST_LOOKUP_POOL_SIZE = 40

# Telegram credentials — supplied via GitHub Actions secrets, never hardcode here.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Finnhub API key (free tier, finnhub.io/register) — supplied via GitHub
# Actions secrets, never hardcode here. Used for per-symbol earnings-date/
# timing lookups (classify_holdings_for_date, and the "earnings for X"
# not-reporting-today check), which don't need market cap. The market-wide
# top-by-market-cap reporters list still uses Nasdaq's calendar (see
# fetch_earnings_calendar in earnings_utils.py) since Finnhub's free
# earnings-calendar endpoint doesn't include market cap.
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "")

STATE_FILE = "state.json"
