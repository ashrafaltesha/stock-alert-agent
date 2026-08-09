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

# -- Per-holding earnings reminders + release watcher (earnings_watch.py) --
#
# Nasdaq's calendar only tells us before-open / after-close / "not
# supplied" -- not an exact release minute -- so these are reasonable
# assumptions, not confirmed company schedules. Adjust per your experience
# with a given holding (e.g. if one of your tickers reliably reports later).
#
# Day-before reminder time (local ET, 24h "HH:MM") for holdings reporting
# before market open the next day.
EARNINGS_BMO_REMINDER_TIME_ET = "18:00"
# When to start polling for a before-market-open release.
EARNINGS_BMO_POLL_START_ET = "06:30"
# Same-day reminder time for holdings reporting after market close.
EARNINGS_AMC_REMINDER_TIME_ET = "15:00"
# When to start polling for an after-market-close release (assumes released
# at/soon after the 4:00pm close).
EARNINGS_AMC_POLL_START_ET = "16:00"
# How often to re-check for the release once polling starts.
EARNINGS_POLL_INTERVAL_SECONDS = 60
# Give up (and send one heads-up that it's still not detected) after this
# many minutes of polling.
EARNINGS_POLL_TIMEOUT_MINUTES = 180

# -- Market-wide earnings watcher (market_earnings_watch.py) --
#
# How many top market-wide earnings reporters to watch, ranked by market cap.
TOP_N_EARNINGS = 10

# How many additional companies to watch based on analyst attention (number
# of analysts covering the stock), among that day's earnings reporters that
# aren't already in the market-cap list above.
TOP_N_ANALYST_ATTENTION = 5

# Cap on how many of the day's reporters get an analyst-coverage lookup.
# Keeps the job fast/reliable on busy earnings days (100+ companies reporting)
# by only checking analyst counts for the largest-cap subset.
ANALYST_LOOKUP_POOL_SIZE = 40

# Time (local ET, "HH:MM") to send the heads-up list of today's top
# market-wide earnings reporters + most-analyst-attention names.
MARKET_EARNINGS_LIST_TIME_ET = "15:55"
# Time to start polling for each of those companies' releases (assumes most
# after-close reporters release at/soon after the 4:00pm close).
MARKET_EARNINGS_POLL_START_ET = "16:00"

# Telegram credentials â supplied via GitHub Actions secrets, never hardcode here.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Finnhub API key (free tier, finnhub.io/register) â supplied via GitHub
# Actions secrets, never hardcode here. Used for per-symbol earnings-date/
# timing lookups (classify_holdings_for_date, and the "earnings for X"
# not-reporting-today check), which don't need market cap. The market-wide
# top-by-market-cap reporters list still uses Nasdaq's calendar (see
# fetch_earnings_calendar in earnings_utils.py) since Finnhub's free
# earnings-calendar endpoint doesn't include market cap.
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "")

STATE_FILE = "state.json"
