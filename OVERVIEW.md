# stock-alert-agent — what it is and how it works

A personal stock assistant that runs entirely on GitHub Actions and talks to
you through Telegram. There is no server, no database, and no hosting bill.
Scheduled jobs wake up, do a small amount of work, message you if something
matters, write down what they saw, and exit.

It does four things:

1. **Watches prices** and tells you when one of your stocks moves sharply.
2. **Watches news** and tells you when something material is published.
3. **Watches earnings** and sends you what a company publishes on the day it
   reports.
4. **Tracks your portfolio** — positions, cash, and realised gains — which you
   update by texting it in plain English.

---

## 1. How it runs

Everything is a scheduled GitHub Actions workflow. Each run is a fresh Linux
machine that lives for under a minute. Nothing persists in memory between
runs, so anything that must be remembered is written to a file and committed
back to the repository.

That constraint shapes the whole design. There is no long-running process, so
there are no timers, no in-memory caches, and no "wait until 4pm" sleeps —
those were removed after they held runners idle for hours. Every job asks
"what time is it, and is there anything to do right now?", acts, and exits.

### Where the code and data live

The code repository is **public**, which matters for a practical reason:
GitHub gives public repositories unlimited free Actions minutes. That is what
makes minute-by-minute polling free.

Your **positions are not public**. `holdings.json` lives in a separate private
repository (`stock-alert-agent-data`) that gets checked out into `data/`
during each run using a token scoped to it alone. Share counts and cost basis
never touch the public repo, and the public repo's history was scrubbed to
remove an earlier copy.

| File | Repo | Contents |
|---|---|---|
| `tickers.json` | public | stocks you own |
| `watchlist.json` | public | stocks you follow but don't own |
| `state.json` | public | what the bot has already seen and said |
| `holdings.json` | **private** | shares, average cost, cash, deposits, realised P&L |

### The two schedules

| Workflow | Cadence | Job |
|---|---|---|
| `monitor.yml` | ~every minute | prices and news |
| `telegram_commands.yml` | every 2 minutes | reads your messages, runs earnings watches |

GitHub's own cron is unreliable at short intervals, so an external scheduler
(cron-job.org) triggers both far more punctually than GitHub's built-in
timer manages.

---

## 2. Price alerts

Each run compares the current price against **yesterday's close** and alerts
at every 5% step away from it: +5%, +10%, +15%, and the same downward.

The anchor is deliberately fixed to the prior close and does not move during
the day. An earlier version re-anchored to the price at each alert, which
meant a stock grinding steadily upward triggered repeatedly on the same move.
Anchoring to yesterday's close makes "+10%" mean the same thing all day.

Each step fires once per day. Watchlist stocks get identical treatment to
holdings — the only difference is that watchlist entries aren't part of
position tracking.

---

## 3. News alerts

Two sources are polled per stock: **Yahoo Finance** and **Google News**. Both
are kept deliberately, because a check of their overlap found only about 44%
of stories appeared in both — each catches things the other misses.

### Making the search accurate

Google News is searched by **company name as a quoted phrase**, not by ticker.
Searching "FOUR stock" or "APP stock" returned huge amounts of unrelated
material, since those tickers are ordinary English words. Company names are
resolved once and cached.

### Deciding what's worth sending

Headlines are matched against a keyword list covering analyst actions, M&A and
partnerships, delivery and production figures, regulatory decisions, and
guidance changes. Everything else is recorded for deduplication but not sent.

### Not telling you twice

Three separate mechanisms, because duplicates kept arriving through different
routes:

- **Per-source IDs** — each article's identifier is stored so the same article
  isn't re-sent by the same source.
- **Fuzzy title matching** — Yahoo and Google often carry the same wire story
  with slightly different wording, which per-source IDs can't catch.
- **Hashed IDs** — Google's identifiers run to ~270 characters. Storing short
  hashes instead cut `state.json` from 195KB to 24KB.

---

## 4. Earnings

The most-rebuilt part of the system, and worth explaining in full because the
current design is the result of several approaches that failed against real
data.

### What you do

Text the bot:

```
earnings for CBRS
```

Any ticker works — held, watchlisted, or neither. The only check is that live
market data exists for the symbol, which catches typos.

### What it does

It arms a **24-hour watch** and replies:

> 🔔 Watching CBRS for the next 24h. I'll check every minute during
> 16:00-18:00, 06:00-09:00 ET and send you anything they publish that day,
> with a link.

Then, during those two windows only, it checks the company's investor
relations page and sends you **anything dated that day**, each item once, with
its URL.

### Why 24 hours

Detection used to be same-day only. That meant a company reporting pre-market
at 6am required texting the bot overnight. The 24-hour span with two windows
— one straddling the 4pm close, one before the 9:30 open — means you can arm
it the afternoon before and be covered either way.

### Why the date, not keywords

The earlier version tried to judge whether a headline *sounded* like earnings.
It nearly missed the real thing. Cerebras titled its Q2 2026 release:

> "Fast Inference Cloud Business Nearly Quadruples in Second Quarter 2026"

No results word anywhere. On the day a company reports, the release is
essentially the only thing it publishes, so **"dated today" is a far more
reliable signal than "sounds like earnings"** — and needs no tuning.

A consequence: the watch does **not** close on the first article. A routine
morning announcement would otherwise end it and the actual results, hours
later, would never arrive. It runs the full 24 hours and remembers what it
sent.

### Where it looks, in order

1. **The company's RSS feed**, if it has one.
2. **The investor-relations page's HTML** — discovered from the ticker
   (website → the "investor relations" link on it) and cached, then parsed by
   pairing each article link with the nearest date.
3. **News headlines**, as a fallback. This path still uses keyword matching,
   because dozens of articles mention a ticker daily and a date rule would be
   meaningless there. Alerts from it are labelled as such.

### Honest limits

Tested live against ten companies that reported on 13 August 2026, **five
worked end to end** (Tapestry, Dillard's, JD.com, Applied Materials, Cerebras).

The failures aren't parsing problems — they're sites that decline to be read.
Genius Sports serves a reCAPTCHA. AppLovin, Applied Industrial, QXO and
Madison Square Garden Sports return zero content even in a real browser. For
those, you get a news headline about the release rather than the company's own
link.

### Sources that were ruled out

Each was tested rather than assumed, and each failed for a specific reason:

| Source | Why not |
|---|---|
| SEC EDGAR | Returns HTTP 403 to GitHub Actions runners — the SEC blocks datacenter IPs |
| GlobeNewswire, Business Wire | Time out from runners |
| Finnhub `epsActual` | Lagged the Cerebras release by an entire evening |
| FMP press releases | HTTP 402 — paid tier only |
| FMP 8-K feed | Refreshes hourly; too slow to be useful |
| SEC-API.io | Would have worked, at $49/month |

Finnhub is still used for the earnings *calendar* — knowing a report is
scheduled — but only as advisory context. It never blocks a watch, because its
calendar is frequently wrong about recent IPOs and foreign issuers.

---

## 5. Portfolio tracking

You update it by texting in plain English. Everything is stored in the private
repository.

### Commands

| What you send | What happens |
|---|---|
| `added 10 shares of NVDA at 500` | Blends into your average cost, deducts $5,000 from cash |
| `sold 10 shares of NVDA at 600` | Reduces shares, credits cash, records the realised gain |
| `deposited 2000` | Adds cash, counts as money in |
| `withdrew 1000` | Removes both |
| `set cash to 10500` | Correction — doesn't count as a deposit |
| `set deposits to X` | Corrects the money-in total |
| `summary` | Full portfolio: positions, market values, cash, returns |
| `add GENI to my list` / `to my watchlist` | Manages the two lists |
| `watchlist` | Every watched symbol with price and daily move |
| `earnings today` | The day's biggest reporters by market cap and analyst coverage |

### How the money works

**Cash is a real balance.** Buying deducts, selling credits, deposits and
withdrawals adjust it directly.

**Purchases you can't afford imply a deposit.** If a buy costs more than the
cash on hand, the shortfall is recorded as a deposit rather than letting the
balance go negative — the money had to come from somewhere for the trade to
have happened. The reply tells you the amount assumed.

**Money in is tracked separately** so returns mean something. Book value
ignores money added along the way, so "am I actually up?" can only be answered
against what you've actually put in. The first `set cash` seeds this with your
cash plus the cost of what you already hold, since those shares were bought
before the bot existed.

**Average cost basis** is used throughout: buying blends the new lot into your
average, selling leaves the average untouched. That's standard — selling
doesn't change what you paid for what remains.

---

## 6. The engineering that keeps it honest

Several problems in this system were invisible failures — things that looked
fine while silently not working. The fixes are worth knowing about, because
they explain design choices that otherwise look over-elaborate.

### Duplicate replies

Both main workflows shared a single concurrency group that allowed one run at
a time. Once both moved to roughly one-minute cadences, about two runs a
minute were arriving into a lane serving one — each taking 30 to 60 seconds.
The queue never drained and GitHub cancelled **163 runs** to clear it.

A cancelled Telegram run had usually already sent your reply but was killed
before recording it as handled, so the next run answered the same message
again.

The tell was subtle: those runs show as **cancelled**, not **failed**, so
filtering the run list by failures shows nothing wrong.

Fixed by giving each workflow its own lane and slowing the Telegram job to two
minutes — a run must finish before the next one starts.

### Two jobs, one state file

Separate lanes meant both could push at once, and the loser's push is
rejected. Rebasing can't work: both sides rewrite every line of the same JSON.

So `merge_state.py` merges the **data** instead — take what's on the remote,
re-apply only what this run changed. Deletions are handled explicitly, because
a resolved earnings watch that got revived by a merge would send the same
alert again.

### Telegram silently dropping alerts

An unbalanced `_` or `*` in a news headline made Telegram reject the entire
message with HTTP 400, and the alert vanished with no trace. External text is
now escaped, failures are logged loudly, and a rejected message is retried as
plain text rather than lost.

### Tests

`tests/test_core_logic.py` covers the logic that has actually broken:
command parsing, Markdown escaping, news deduplication, earnings-window
timing, the state merge, and the article extractor — including a regression
test for a scheduling notice that would otherwise be mistaken for results.

---

## 7. Known limitations

- **Timing isn't real-time.** Runs are every one to two minutes, and GitHub's
  scheduler can lag. Treat it as "within a few minutes", not instant.
- **Earnings coverage is roughly half.** Some IR sites cannot be read
  automatically; those fall back to news headlines.
- **Alerts carry headlines and links, not figures.** The runner can't reliably
  fetch article bodies, so you get the release fast and read the numbers
  yourself.
- **Keyword filtering is not comprehension.** The material-news list is simple
  string matching and will let some noise through while missing some signal.
- **Nothing here is investment advice.** It's a notification and
  record-keeping system. Every judgement about what the information means is
  yours.
