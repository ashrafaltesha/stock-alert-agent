"""
Central configuration for the stock alert agent.
Edit TICKERS to change which stocks you hold.
"""

import os

# Tickers you own — edit this list any time.
TICKERS = ["GENI", "EVH", "XPEV", "QURE", "UAVS", "WOLF", "FOUR"]

# Alert threshold: percent move that triggers a price alert. Alerts fire on
# every sequential move of this size in either direction (e.g. at 5%: +5%
# from previous close alerts, then +/-5% from THAT point alerts again, and
# so on through the day).
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

# How many top market-wide earnings movers to include in the daily report,
# ranked by market cap.
TOP_N_EARNINGS = 10

# How many additional companies to surface based on analyst attention
# (number of analysts covering the stock), among that day's earnings reporters.
TOP_N_ANALYST_ATTENTION = 10

# Cap on how many of the day's reporters get an analyst-coverage lookup.
# Keeps the job fast/reliable on busy earnings days (100+ companies reporting)
# by only checking analyst counts for the largest-cap subset.
ANALYST_LOOKUP_POOL_SIZE = 40

# Telegram credentials — supplied via GitHub Actions secrets, never hardcode here.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

STATE_FILE = "state.json"
"""
Central configuration for the stock alert agent.
Edit TICKERS to change which stocks you hold.
"""

import os

# Tickers you own — edit this list any time.
TICKERS = ["GENI", "EVH", "XPEV", "QURE", "UAVS", "WOLF", "FOUR"]

# Alert threshold: percent move from previous close that triggers a price alert.
PRICE_CHANGE_THRESHOLD_PCT = 5.0

# How far back to look for "new" news articles on each run (minutes).
# Should be >= the cron interval (15 min) with a little buffer.
NEWS_LOOKBACK_MINUTES = 20

# How many top market-wide earnings movers to include in the daily report,
# ranked by market cap.
TOP_N_EARNINGS = 10

# How many additional companies to surface based on analyst attention
# (number of analysts covering the stock), among that day's earnings reporters.
TOP_N_ANALYST_ATTENTION = 10

# Cap on how many of the day's reporters get an analyst-coverage lookup.
# Keeps the job fast/reliable on busy earnings days (100+ companies reporting)
# by only checking analyst counts for the largest-cap subset.
ANALYST_LOOKUP_POOL_SIZE = 40

# Telegram credentials — supplied via GitHub Actions secrets, never hardcode here.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

STATE_FILE = "state.json"
