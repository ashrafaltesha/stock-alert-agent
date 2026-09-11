# How news alerting works, and where it still falls short

Written 2026-09-11, after a fortnight in which the news path failed three
separate ways and every Actions run stayed green throughout.

[`README.md`](README.md) is the map, [`OVERVIEW.md`](OVERVIEW.md) explains
the system as a whole, [`EARNINGS.md`](EARNINGS.md) covers earnings
detection. This is the equivalent deep dive for news, which is now the
weakest of the three alert paths and the one with the most measured evidence
behind it.

---

## Part 1 — What the system does today

### The shape of it

`monitor.py` runs on a schedule, collects candidate articles for every
ticker in `tickers.json` and `watchlist.json`, screens them in three stages,
and sends what survives to Telegram.

```
for each ticker:
    check_yahoo_news()    -> candidates
    check_google_news()   -> candidates

process_news_candidates(candidates, state):
    stage 1  SOURCE     blocked publishers and listicle shapes   (no network)
    stage 2  DEDUPE     already-sent, across both feeds          (no network)
    stage 3  CLASSIFY   one batched model call for the remainder
    send what passes both halves of should_alert()
```

Cheapest first, deliberately: each stage removes work from the next, and
stage 3 is the only one that costs anything.

### The two sources

**Yahoo Finance**, via `yfinance`'s `.news` property. Returns items with an
id, a title, a publisher and a timestamp under one of `pubDate`,
`providerPublishTime` or `displayTime` — the schema has changed across
yfinance releases, so all three are tried and an unparseable value is
treated as *fresh* rather than stale. Treating it as stale would silently
drop real news; treating it as fresh risks one redundant alert.

**Google News RSS**, queried by **resolved company name as a quoted
phrase**, not by ticker. `"FOUR stock"` and `"APP stock"` returned mostly
unrelated articles because those tickers are ordinary words. The company
name comes from `company_name::TICKER` in `state.json`, resolved once via
yfinance and cached.

Both are kept because a measurement of their overlap found only ~44% of
stories appeared in both.

### The freshness window

```python
NEWS_LOOKBACK_MINUTES = 360          # config.py
ID_RETENTION_MINUTES  = 360 * 3      # monitor.py
```

An article alerts only if it was published within the last **6 hours** when
first seen. This is not about avoiding duplicates — dedup handles that — it
exists so that adding a new ticker doesn't dump a backlog on you.

Seen-article ids are remembered for 3× the window and then forgotten. Only
articles **inside** the window are recorded at all: one outside it can never
alert, so storing it buys nothing. That rule cut `state.json` from 94 KB to
roughly 6 KB.

### The three filter stages

**Stage 1 — source and shape** (`news_filter.source_allowed`). Twenty-odd
blocked publishers (`zacks`, `motley fool`, `simplywall.st`, `marketbeat`,
`tipranks`, `benzinga insights`…) plus `_NOISE_RE`, a regex covering
listicles, "stocks to watch", institutional-ownership churn, single-broker
price-target tweaks and valuation opinion. **Every pattern came from an
alert this bot actually sent.**

Publishers are matched in **both spaced and domain form** — Google reports
the outlet's own display name, and for several of these that is the domain
(`simplywall.st`, not "Simply Wall St"). Matching only the prose form let
that publisher through entirely.

**Stage 2 — dedup.** Three mechanisms, because duplicates arrived three
ways:

- `seen_news::TICKER` / `seen_news_google::TICKER` — per-source article ids,
  stored as 16-char SHA-1 hashes (Google's raw ids run to ~270 characters).
- `alerted_titles::TICKER` — the last 40 headlines sent, compared with
  `difflib` at a 0.82 similarity threshold, catching the same wire story
  worded differently across outlets.
- In-batch dedup, because both feeds routinely carry one story in one run.

**Stage 3 — classification.** One batched model call (Groq, Gemini
fallback) returning `subject`, `impact`, `event`, `why` per headline.

```python
send  =  verdict["subject"] is True  AND  verdict["impact"] == "high"
```

**Both halves are required.** Impact alone passes "chip stocks surge on AI
demand" — real news, about somebody else. Subject alone passes every product
blog post. With no model key this degrades to keyword matching rather than
to silence.

### Delivery

An alert carries ticker, event label, headline, publisher and link, with all
external text passed through `escape_markdown` — an unbalanced `_` or `*` in
a headline once made Telegram reject an entire message with HTTP 400 and the
alert vanished without trace.

---

## Part 2 — What broke, and what it cost

Four failures in a fortnight. Every one was invisible in a green run.

### 2.1 The window was calibrated against the wrong thing

`NEWS_LOOKBACK_MINUTES` was **70**, sized to the off-hours cron interval on
the assumption that the risk was missing something *between* runs. Wrong
question — the monitor ran every minute. What matters is how stale an item
already is when the feed first surfaces it.

Measured on 2026-09-08, reading the same queries the bot sends:

```
"XPeng Inc."             100 items   freshest 155 min   0 within 70 min
"Meta Platforms, Inc."   100 items   freshest  89 min   0 within 70 min
"Applovin Corporation"   101 items   freshest 153 min   0 within 70 min
"Genius Sports Limited"  100 items   freshest 560 min   0 within 360 min
```

**Google News RSS ranks by relevance, not recency.** Its freshest item is
routinely 1–3 hours old and the rest of the feed is 20–45 hours old. Against
that distribution a 70-minute gate admitted essentially nothing. For three
days every run logged `0 candidate article(s)` and nothing said anything was
wrong.

Now 360, which sits at the natural break in the distribution.

### 2.2 Overlapping runs both alerted

You received the same AppLovin story twice, 45 seconds apart:

```
01:08:01  run A starts, checks out dcdd713   (11 alerted titles)
01:09:01  run B starts, checks out dcdd713   (same snapshot)
01:09:05  run A sends
01:09:07  run A pushes 212101a               (12 titles)
01:09:50  run B sends THE SAME STORY
01:09:52  run B pushes 7ca9034               (still 12 titles)
```

The merge was never wrong — exactly one copy of the title survived. Both
*decisions* were made against a read that predated the other run's write, so
no amount of merging on push could have prevented the second message.

Fixed by `refresh_alerted_titles()`, which re-reads `origin/main`'s dedup
lists at the moment of decision and unions them into the in-memory state.
The window narrowed from ~40 seconds to one fetch.

**This race predated the window change but was unreachable while candidates
never appeared.** Widening the window is what made it reachable.

### 2.3 Google rate-limited every ticker

At a one-minute cadence across ~15 tickers the bot was making roughly **900
RSS requests an hour** from one runner IP:

```
[APP  google-news] HTTP 429 (attempt 1/3, 2/3, 3/3) -- giving up
[BAK  google-news] HTTP 429 ...
[BRZE google-news] HTTP 429 ...
... every ticker
```

For stretches of the day the system was running on Yahoo alone — half the
coverage the design assumes. Mitigated by moving the external cron trigger
from 1 minute to 5.

### 2.4 The company's own newsroom is invisible

Genius Sports published the Equativ/StackAdapt partnership on their site on
10 September. It reached Google **16.6 hours** later. The bot has no ability
to read company websites at all — the IR-page scraper was deleted in August
because it worked for only 5 of 10 companies tested.

---

## Part 3 — The Alpaca evaluation, and why it was rejected

Worth recording because the reasoning was sound and the conclusion was still
wrong.

Alpaca's news API is Benzinga-backed, symbol-tagged, free on the Basic plan
(30 websocket symbols, 200 calls/min), and pushes over a websocket with
honest RFC-3339 timestamps. Every architectural argument said it should beat
a relevance-ranked RSS scrape: no freshness heuristic needed, no
company-name query guessing, no 429s.

Measured over 7 days against the live portfolio:

```
        ALPACA          GOOGLE
        arts   newest   arts   newest
  APP      5    74.8h    100     2.1h
  BAK      0       --    100    10.3h
  BRZE    22    50.6h    100    28.8h
  CENX     0       --    100    95.1h
  EVH      0       --    100    70.5h
  FOUR     2    26.8h    100    20.2h
  GENI     1    72.2h    100    16.6h
  HE       1    76.4h      1   685.4h
  KEEL     1    54.3h    100     1.4h
  QURE     0       --    100     0.9h
  RDDT     9     5.8h    100     8.5h
  UAVS     0       --    100   197.3h
  WOLF     0       --    100   147.9h
  XPEV     5    50.8h    100    31.8h
```

**Coverage:** six of fourteen holdings returned nothing in seven days. Four
are positions, not watchlist names.

**Latency:** the opposite of the prediction. Alpaca was consistently slower
on these tickers — APP 74.8h against Google's 2.1h.

**Content:** the sample it returned was largely Benzinga's automated
listicles ("9 Communication Services Stocks With Whale Alerts In Today's
Session") — exactly what stage 1 discards.

Rejected. The probe cost one file and an afternoon; rebuilding the collector
on the architectural argument would have cost the coverage of six tickers.

---

## Part 4 — Suggestions, ordered by value per unit of risk

### 4.1 Make silence audible — HIGHEST value, LOW risk

Every failure above was found because *you* noticed the bot was quiet. That
is the only detector currently in place, and it has a latency measured in
days.

Two pieces, both small:

**A failure notifier.** Five lines of `if: failure()` per workflow, posting
the workflow name and run URL to Telegram using the bot token already in
every one of them. When `DATA_REPO_PAT` expired on 11 September the listener
failed **every ten minutes for sixteen hours** with conclusion `failure` —
the most detectable state a workflow can be in — and nothing said a word.

**A news staleness alarm.** If no candidate has survived stage 1 from *any*
feed for a full trading day, say so. That is the shape of 2.1 and it went
unreported for three days.

### 4.2 Instrument the collectors — HIGH value, ZERO risk

Today a run logs exactly one line:

```
0 candidate article(s) this run.
```

That cannot distinguish "no news exists" from "both feeds returned nothing"
from "everything was filtered". During 2.1 I had to read the feeds myself,
from outside the system, to work out which. Print per ticker per feed: raw
items returned, how many were new, how many were in-window, and **the age of
the newest item**. That last number would have diagnosed 2.1 in one run.

### 4.3 Read company newsrooms — HIGH value, MEDIUM risk

The measured gap: GENI's launch was on their site immediately, on Google at
16.6h, on Alpaca at 72.2h. **The fastest source is the one not being read.**

Not a revival of the old auto-discovery scraper, which failed on half the
sites it tried. Instead a short per-ticker map of known-good newsroom URLs,
checked like any other feed. `geniussports.com/newsroom` is clean
server-rendered HTML with dates and titles — I read it without JavaScript.
Add tickers to the map only when their page is verified to parse; anything
absent simply falls back to today's behaviour.

Risk is real: it touches the alert path and adds per-site parsing that can
rot silently. It should ship with the instrumentation in 4.2 so that rot is
visible.

### 4.4 Widen the window to 24 hours — MEDIUM value, LOW risk

6 hours was calibrated on APP, META and XPEV, whose freshest items sit at
89–155 minutes. It is too tight for the smaller names: GENI had **zero**
items within 6 hours and four within 24.

Dedup guarantees one alert per article regardless, so the cost is breadth,
not repetition. The one behaviour change is that a newly added ticker
surfaces up to a day of backlog on its first run — which 4.5 removes.

### 4.5 Silent baseline on a ticker's first run — MEDIUM value, LOW risk

The only thing the window genuinely protects against is a backlog dump when
a ticker is added. Seeding `seen_news::` silently on first sight removes
that concern entirely and lets the window be set on merit rather than as a
blunt guard.

### 4.6 Price alerts share the news race — MEDIUM value, LOW risk

`check_price_moves` reads `price_ref::` from the same checkout snapshot that
caused 2.2. It has not bitten because a price crossing a threshold twice
inside one interval is rarer than an article appearing, but the shape is
identical and the fix is the same `refresh` call.

### 4.7 Retire `FMP_API_KEY` — LOW value, ZERO risk

Still present in repository secrets; the FMP press-release path was ruled
out (HTTP 402, paid tier) and deleted. A credential that outlives its code
is a small liability and a large source of confusion later.

---

## Part 5 — What NOT to change

- **Two sources.** Their overlap was measured at ~44%.
- **Requiring subject AND high impact.** Either alone was tested and floods.
- **Querying Google by company name, not ticker.** FOUR, APP and WOLF are
  ordinary words.
- **Blocked-publisher matching in both spaced and domain form.** Google
  reports whichever the outlet uses.
- **Treating an unparseable timestamp as fresh.** The opposite silently
  drops real news.
- **The 0.82 fuzzy-title threshold.** Tuned against real duplicate wire
  stories; tightening it lets near-identical rewrites through.

---

## Suggested order

| # | Item | Effort | Payoff |
|---|---|---|---|
| 1 | Failure notifier (4.1) | 30 min | Every future failure visible in minutes |
| 2 | Collector instrumentation (4.2) | 45 min | "0 candidates" becomes diagnosable |
| 3 | News staleness alarm (4.1) | 45 min | A quiet feed reports itself within a day |
| 4 | 24-hour window + baseline (4.4, 4.5) | 30 min | Catches the GENI class of story |
| 5 | Newsroom reader (4.3) | 3–4 h | The fastest source, finally read |
| 6 | Price-alert refresh (4.6) | 30 min | Closes the same race on the other path |
| 7 | Retire FMP secret (4.7) | 5 min | One less stale credential |

Items 1–3 are pure observability and touch no alerting logic. Item 5 is the
only one that should not be combined with anything else.
