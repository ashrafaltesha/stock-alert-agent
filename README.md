# stock-alert-agent

A personal stock assistant that runs entirely on GitHub Actions — no server,
no hosting cost — and talks to you through Telegram.

It watches the stocks you own and the ones you're just interested in, and
messages you when something actually happens: a meaningful price move, a
material news headline, or an earnings release.

---

## What it sends you, unprompted

**Price moves.** During market hours only, you get an alert when a stock is
`PRICE_CHANGE_THRESHOLD_PCT` (default 5%) or more away from *yesterday's
close*. The anchor is fixed, not a moving checkpoint: you get one alert at
5%, another if it reaches 10%, 15%, and so on — each threshold once per day
per direction, reset every morning against the new previous close.

**Material news.** Around the clock, from two independent sources: Yahoo
Finance's feed and Google News RSS. Only headlines matching
`MATERIAL_NEWS_KEYWORDS` are forwarded — analyst upgrades/downgrades, M&A,
partnerships, delivery and production numbers, FDA approvals, guidance,
contract wins. Everything else is recorded silently so it's never
re-evaluated.

**Earnings, for holdings only.** A day-before reminder for anything
reporting before the next open, a same-day reminder for after-close
reporters, then repeated short checks for the release itself, with a
beat/miss summary sent as soon as it's detected.

---

## Commands you can text it

| Command | What it does |
|---|---|
| `add TICKER to my list` | Adds to your holdings list (`tickers.json`) |
| `remove TICKER from my list` | Removes from the holdings list |
| `add TICKER, TICKER to my watchlist` | Adds to `watchlist.json` — alerts without ownership |
| `remove TICKER from my watchlist` | Removes from the watchlist |
| `watchlist` | Lists watchlist symbols with price and % move |
| `added N shares of TICKER at $X` | Blends into your weighted-average cost basis |
| `sold N shares of TICKER at $X` | Reduces shares; tracks cash proceeds and realized P&L |
| `summary` | Full portfolio: shares, avg cost, live price, % up/down, totals |
| `earnings today` | The day's biggest reporters by market cap and analyst attention |
| `earnings for TICKER, ...` | Checks the calendar, then polls and reports beat/miss |

Commands are case-insensitive and a leading `$` on a ticker is optional.
Only messages from `TELEGRAM_CHAT_ID` are processed; anything else is
ignored.

### Holdings list vs. watchlist vs. positions

Three separate things, easy to conflate:

- **`tickers.json`** — the holdings list. Gets price alerts, news alerts,
  and earnings reminders.
- **`watchlist.json`** — symbols you don't own. Identical price and news
  alerting, but **no** earnings reminders and no position tracking.
- **`holdings.json`** — the actual position ledger (shares, average cost).
  Lives in a **separate private repo**; see below.

---

## Repository layout

This repo is **public** and holds only code plus non-sensitive data.

```
stock-alert-agent          (public)   code, tickers.json, watchlist.json, state.json
stock-alert-agent-data     (private)  holdings.json  <- shares and cost basis
```

Workflows that need positions check out the private data repo into `data/`
using the `DATA_REPO_PAT` secret, and commit changes back to it separately
from this repo. `holdings.json` is gitignored here and was scrubbed from
this repo's history — it must never be committed to the public repo.

---

## Workflows

| Workflow | Script | Schedule |
|---|---|---|
| `monitor.yml` | `monitor.py` | Every minute (cron-job.org) + native cron backup |
| `telegram_commands.yml` | `telegram_commands.py` | Every minute (cron-job.org) + native cron backup |
| `earnings_bmo_reminder.yml` | `earnings_watch.py bmo_reminder` | Every 10 min, 22:00–23:59 UTC, weekdays |
| `earnings_watch_premarket.yml` | `earnings_watch.py premarket_watch` | Every 5 min, 10:00–14:59 UTC, weekdays |
| `earnings_watch_afterhours.yml` | `earnings_watch.py afterhours_watch` | Every 5 min, 19:00–23:59 UTC, weekdays |
| `earnings_market_watch.yml` | `market_earnings_watch.py` | Manual only |
| `simulate.yml` | `simulate.py` | Manual only |
| `tests.yml` | `pytest` | On push to code, and manual |

**Why the earnings workflows fire repeatedly rather than once.** They used
to start once and sleep on the runner for hours waiting for a release. They
now exit immediately if their window hasn't opened and otherwise make a
single detection pass, keeping progress in `state.json`. A long-sleeping
job could be evicted and lose everything, and — more importantly — it would
hold the shared concurrency group and stall the one-minute pollers behind
it.

**Concurrency.** Every workflow that writes to the repo shares the
`stock-alert-agent-repo-write` group with `cancel-in-progress: false`, so
they queue instead of racing to push. `tests.yml` writes nothing and stays
out of the group.

---

## Setup

**Secrets** (Settings → Secrets and variables → Actions):

| Secret | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From @BotFather |
| `TELEGRAM_CHAT_ID` | Your chat id — the only one the bot will answer |
| `FINNHUB_API_KEY` | Earnings calendar and release detection |
| `DATA_REPO_PAT` | Fine-grained PAT, Contents read/write, scoped to the private data repo only |

**External trigger.** GitHub's own cron is best-effort and can lag several
minutes. cron-job.org calls the `workflow_dispatch` endpoint for
`monitor.yml` and `telegram_commands.yml` every minute; the native
`schedule:` blocks remain as a free fallback. Because this repo is public,
Actions minutes are unlimited, which is what makes one-minute polling
viable.

---

## Data sources and their limits

- **yfinance** — prices and Yahoo's news feed. Pinned in
  `requirements.txt`; its news schema has changed across releases before,
  so upgrade deliberately and confirm alerts still arrive.
- **Google News RSS** — broader coverage. Queried by resolved company name
  rather than `"TICKER stock"`, because tickers that are ordinary words
  (FOUR, WOLF, APP) returned a lot of unrelated articles.
- **Finnhub** — earnings calendar and actual EPS/revenue. Beat/miss is
  EPS-based; revenue can lag briefly behind EPS in the calendar data.

All three are rate-limitable, so calls go through `http_utils.py`, which
retries 429/5xx with exponential backoff and logs loudly rather than
failing silently.

---

## Known limitations

- The material-news filter is plain keyword matching, not classification.
  If it's too noisy or too quiet, tune `MATERIAL_NEWS_KEYWORDS` in
  `config.py`.
- No market-holiday calendar. On holidays the price check runs and finds
  nothing moving; harmless, just wasted runs.
- The after-hours window is defined in UTC. Under EST the tail of a very
  late release can fall outside it, delaying the "not detected" notice to
  the next weekday run.
- `state.json` is committed on nearly every run, so this repo's history is
  mostly bot commits. Article ids are hashed to keep the file small.
- Telegram messages use Markdown. External text is escaped, and a failed
  send is retried once as plain text so an alert is never silently lost.

---

## Development

```bash
pip install -r requirements.txt
pip install pytest
python -m pytest tests/ -q
```

The test suite is deliberately narrow: command regexes, Markdown escaping,
news-id hashing and migration, headline dedup, and the keyword filter — the
pure logic that has actually broken before. No network, no secrets.

To exercise a workflow by hand, use **Actions → (workflow) → Run workflow**.
The earnings workflows accept a `test_mode` input that skips the
window guard.
