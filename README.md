# stock-alert-agent

A personal stock assistant that runs entirely on GitHub Actions — no server,
no hosting cost — and talks to you through Telegram.

It watches the stocks you own and the ones you're only interested in, and
messages you when something actually happens: a meaningful price move, news
that would change your mind, or an earnings release.

**Start here:** this file is the map — what it does, how to run it, where
things live. [`OVERVIEW.md`](OVERVIEW.md) explains how the whole system works
and why it's built this way. [`EARNINGS.md`](EARNINGS.md) is the deep dive on
earnings detection, which is the most involved part.

---

## What it sends you, unprompted

**Price moves.** During market hours, an alert when a stock is
`PRICE_CHANGE_THRESHOLD_PCT` (default 5%) or more from *yesterday's close*.
The anchor is fixed, not a moving checkpoint: one alert at 5%, another at
10%, 15%, and so on — each threshold once per day per direction, reset every
morning against the new previous close.

**News that matters.** Around the clock, from Yahoo Finance and Google News.
Screened in three stages — blocked sources and listicle shapes, then dedup
across both feeds, then one batched model call asking whether the company is
the primary subject and whether a holder would act on it today. Only
company-specific, high-impact items are sent; everything else is logged and
dropped. See [`news_filter.py`](news_filter.py).

**Earnings, within seconds.** SEC filings are polled every 15 seconds while a
company is being watched. You get the filing and its link the moment it
appears, then a second message with revenue, EPS, margins, guidance, and
beat/miss against consensus. Full detail in [`EARNINGS.md`](EARNINGS.md).

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
| `sold N shares of TICKER at $X` | Reduces shares; tracks cash proceeds and realised P&L |
| `deposited N` / `withdrew N` | Adjusts cash and money-in |
| `set cash to N` / `set deposits to N` | Corrections that don't count as deposits |
| `summary` | Full portfolio: shares, avg cost, live price, % up/down, totals |
| `earnings today` | The day's biggest reporters by market cap and analyst attention |
| `earnings for TICKER, ...` | Arms a 24-hour SEC filing watch on any ticker |

Case-insensitive; a leading `$` is optional. Only messages from
`TELEGRAM_CHAT_ID` are processed — anything else is ignored and logged.

Replies arrive in **one to two seconds**. A listener holds an open long-poll
connection to Telegram rather than checking on a schedule.

### Holdings list vs. watchlist vs. positions

Three separate things, easy to conflate:

- **`tickers.json`** — the holdings list. Price alerts, news alerts, and
  automatic earnings watches.
- **`watchlist.json`** — symbols you don't own. Identical price and news
  alerting, but **no** automatic earnings watch and no position tracking.
- **`holdings.json`** — the position ledger (shares, average cost, cash).
  Lives in a **separate private repo**.

---

## Repository layout

This repo is **public** and holds only code plus non-sensitive data.

```
stock-alert-agent          (public)   code, tickers.json, watchlist.json, state.json
stock-alert-agent-data     (private)  holdings.json  <- shares and cost basis
```

Workflows that need positions check out the private data repo into `data/`
using the `DATA_REPO_PAT` secret and commit back to it separately.
`holdings.json` is gitignored here and was scrubbed from this repo's history
— **it must never be committed to the public repo.**

Public isn't incidental. Public repositories get unlimited free Actions
minutes, which is what makes an always-on listener and a 15-second earnings
poll cost nothing.

---

## Workflows

| Workflow | Script | Schedule |
|---|---|---|
| `monitor.yml` | `monitor.py` | Every 5 min during market hours, hourly otherwise |
| `telegram_commands.yml` | `telegram_commands.py listen` | Hourly; each run listens for 62 min |
| `earnings_watch.yml` (arm) | `earnings_watch.py arm` | 09:00 and 19:00 UTC, weekdays |
| `earnings_watch.yml` (watch) | `earnings_watch.py poll` | Hourly 09:05–23:00 UTC weekdays, plus on demand |
| `simulate.yml` | `simulate.py replay` | Manual only |
| `tests.yml` | `pytest` | On push to code, and manual |

**Two long-running jobs, not many short ones.** The Telegram listener and the
earnings watcher each loop for 62 minutes against a 60-minute restart, so the
incoming run overlaps the outgoing one. Both replaced per-poll designs that
spent 40–100 seconds provisioning a machine to do a few seconds of work.

**Concurrency.** Each workflow has its own lane. The two loops use
`cancel-in-progress: true` so a new run replaces the old; `monitor.yml` uses
`false` so runs queue. Sharing one lane is what caused 163 cancelled runs and
the duplicate-reply bug — see [`OVERVIEW.md`](OVERVIEW.md).

---

## Setup

**Secrets** (Settings → Secrets and variables → Actions):

| Secret | Purpose | Required |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | From @BotFather | yes |
| `TELEGRAM_CHAT_ID` | Your chat id — the only one the bot answers | yes |
| `FINNHUB_API_KEY` | Earnings calendar and consensus EPS | yes |
| `DATA_REPO_PAT` | Fine-grained PAT, Contents read/write, private data repo only | yes |
| `GROQ_API_KEY` | News classification and earnings metric extraction | recommended |
| `GEMINI_API_KEY` | Fallback for the above | optional |

Without a model key the system still runs: news falls back to keyword
matching and earnings alerts quote the release verbatim instead of listing
parsed figures. Both free tiers cover this volume many times over.

**No external scheduler.** cron-job.org used to trigger `telegram_commands`
every two minutes. That must stay **off** — with `cancel-in-progress: true`,
every dispatch kills the running listener.

---

## Data sources and their limits

- **`data.sec.gov`** — the earnings source of record. Reachable from Actions
  runners; the `www.sec.gov` search endpoints are not (403).
- **yfinance** — prices and Yahoo's news feed. Pinned in `requirements.txt`;
  its news schema has changed across releases, so upgrade deliberately.
- **Google News RSS** — queried by resolved company name, not `"TICKER
  stock"`, because tickers that are ordinary words (FOUR, WOLF, APP) returned
  mostly unrelated articles.
- **Finnhub + Nasdaq** — earnings calendars, unioned. Used only to know a
  report is *scheduled*; detection never depends on them.

All calls go through `http_utils.py`, which retries 429/5xx with exponential
backoff and logs loudly rather than failing silently.

---

## Development

```bash
pip install -r requirements.txt
pip install pytest
python -m pytest tests/ -q
```

The suite is deliberately narrow: command regexes, Markdown escaping,
news-id hashing, headline dedup, the state merge, and the filing classifier —
the pure logic that has actually broken. No network, no secrets.

To exercise a workflow by hand: **Actions → (workflow) → Run workflow**.

---

Nothing here is investment advice. It is a notification and record-keeping
system; every judgement about what the information means is yours.
