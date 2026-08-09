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
