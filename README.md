# Stock Alert Agent (Telegram + GitHub Actions)

Sends you Telegram alerts for your holdings (GENI, EVH, XPEV, QURE, UAVS, WOLF, FOUR):
- Breaking news headlines (Yahoo Finance + Google News)
- Price moves of 5%+ from the previous close
- A daily after-market report on earnings: your holdings, the day's biggest reporters by market cap, and reporters getting the most analyst attention

Runs entirely on GitHub Actions' free tier — no server, no cost for normal usage.

## Setup (10 minutes)

1. **Create a new GitHub repo** (private is fine) and upload all the files in this folder, preserving the folder structure (the `.github/workflows/` folder must stay at the repo root).

2. **Add your Telegram credentials as repo secrets:**
   Repo → Settings → Secrets and variables → Actions → New repository secret
   - `TELEGRAM_BOT_TOKEN` → your bot token from @BotFather
   - `TELEGRAM_CHAT_ID` → your chat ID

3. **Enable Actions** if prompted (Actions tab → "I understand my workflows, go ahead and enable them").

4. **Test it manually:** Actions tab → select "Stock price & news monitor" → Run workflow. Check your Telegram — you should see activity in the Action logs even if no alert fires (alerts only fire on real news/moves).

5. Do the same for "Daily earnings report" to test it end to end.

That's it — from here both workflows run on their own schedules:
- Price/news monitor: every 15 min, ~9am–4:15pm ET, weekdays
- Earnings report: ~5:30pm ET, weekdays

## Customizing

- **Tickers**: edit `TICKERS` in `config.py`.
- **Move threshold**: edit `PRICE_CHANGE_THRESHOLD_PCT` in `config.py` (default 5%).
- **Check frequency**: edit the cron in `.github/workflows/monitor.yml` (`*/15` = every 15 min; GitHub's minimum granularity is 5 min, and actual run times can drift a few minutes under load).
- **Report time**: edit the cron in `.github/workflows/earnings_report.yml`. It's set for EST (UTC-5); shift by 1 hour during daylight saving if you want it precise.

## Earnings report structure

Three Telegram messages, sent once daily after market close:
1. **Your holdings** — any of your 7 tickers that reported earnings that day.
2. **Top by market cap** — the `TOP_N_EARNINGS` (default 10) largest companies that reported that day.
3. **Most analyst attention** — up to `TOP_N_ANALYST_ATTENTION` (default 10) additional companies from that day's reporters, ranked by number of covering analysts (`numberOfAnalystOpinions` from Yahoo Finance), excluding anything already shown in the market-cap list. This surfaces names Wall Street is watching closely even if they're not mega-caps.

To keep the daily run fast on high-volume earnings days, analyst-coverage lookups are limited to the `ANALYST_LOOKUP_POOL_SIZE` (default 40) largest-cap reporters that day — all configurable in `config.py`.

## News sources

Each run checks two feeds per ticker and dedupes against what's already been sent:
- **Yahoo Finance** news feed (via `yfinance`)
- **Google News** RSS search for `"<TICKER> stock"` — this aggregates headlines across outlets (Reuters, CNBC, Bloomberg, MarketWatch, Benzinga, Seeking Alpha, Motley Fool, etc.), so you're not limited to whatever Yahoo happens to pick up.

Only headlines published in the last `NEWS_LOOKBACK_MINUTES` (default 20) get sent, so it won't replay old news on first run.

## Known limitations (worth knowing upfront)

- **Data sources are free/unofficial**: price data comes from `yfinance` (unofficial Yahoo Finance wrapper), news from Yahoo + Google News RSS, and the market-wide earnings calendar from Nasdaq's public API. All of these can occasionally rate-limit, change format, or go down — this is a tradeoff for $0 cost. If you want more reliable data, Alpha Vantage or Finnhub have real APIs with generous free tiers and could replace the relevant functions.
- **Google News search is by ticker+"stock"**, not a curated feed — for tickers with generic names this could occasionally surface a loosely-related result. Let me know if you want it tightened to the company's full name instead.
- **Market holidays aren't filtered** — the monitor will still run on holidays like Thanksgiving, it'll just find no meaningful moves/news.
- **After-hours price data** can be sparse or missing for smaller/less-liquid tickers (several of your holdings are lower-cap), since free sources don't always carry robust after-hours quotes.
- **"Breaking" news** is approximated as "any new headline since the last 15-min check" — there's no true real-time push feed in the free tier.
- State (which alerts have already fired today) is stored in `state.json` and committed back to the repo by the workflow — don't edit that file by hand while workflows are running.

## Files

- `config.py` — your tickers, thresholds, settings
- `monitor.py` — price + news check (runs every 15 min)
- `earnings_report.py` — daily earnings report (runs once/day after close)
- `telegram_utils.py` — Telegram send helper
- `market_hours.py` — market-hours check
- `state.json` — persisted alert/dedupe state (auto-updated)
- `.github/workflows/` — the two scheduled workflows
