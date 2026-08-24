# How the whole system works

Written for you, later, having forgotten the details. [`README.md`](README.md)
covers what it does and how to run it; this file covers *why it is shaped this
way*, including the failures that produced each design choice.
[`EARNINGS.md`](EARNINGS.md) goes deeper on earnings specifically.

It does four things:

1. **Watches prices** and tells you when one of your stocks moves sharply.
2. **Watches news** and tells you when something published would change your
   mind.
3. **Watches earnings** and sends you the figures within seconds of filing.
4. **Tracks your portfolio** — positions, cash, realised gains — which you
   update by texting it in plain English.

---

## 1. The execution model

Everything is a GitHub Actions workflow. There is no server and no database.
State that must survive a run is written to a JSON file and committed back to
the repository.

Originally every job was short: wake up, do a few seconds of work, exit. That
model still runs the price and news monitor, but **the two latency-sensitive
paths have moved to long-running loops**, because the short-job model has a
floor that no amount of tuning gets under:

| Cost | Time |
|---|---|
| GitHub queues and provisions a runner | 5–15s |
| `checkout` (×2 where the private repo is needed) | 2–4s |
| `setup-python` | 0–5s |
| `pip install` (yfinance, lxml) | ~13s |
| **The actual work** | **2–8s** |

Roughly 45 seconds of overhead to do 4 seconds of work. Polling faster just
repeats the overhead — and firing faster than a run completes queues runs into
a lane that cancels them.

So the Telegram listener and the earnings watcher each **pay startup once an
hour** and loop. Both run 62 minutes against a 60-minute restart, so the
incoming run overlaps the outgoing one rather than leaving a gap on the hour.

This is only free because the code repo is **public** — public repositories
get unlimited Actions minutes. If it were ever made private, a 24/7 listener
would exhaust the 2,000-minute monthly allowance in about a day and a half.

### Where code and data live

Your **positions are not public**. `holdings.json` lives in a separate private
repo (`stock-alert-agent-data`) checked out into `data/` with a token scoped
to it alone. The public repo's history was scrubbed of an earlier copy.

| File | Repo | Contents |
|---|---|---|
| `tickers.json` | public | stocks you own |
| `watchlist.json` | public | stocks you follow but don't own |
| `state.json` | public | what the bot has already seen and said |
| `cik_map.json` | public | ticker → SEC CIK |
| `holdings.json` | **private** | shares, average cost, cash, deposits, realised P&L |

---

## 2. Price alerts

Each run compares the current price against **yesterday's close** and alerts
at every 5% step away from it: +5%, +10%, +15%, and the same downward.

The anchor is deliberately fixed and does not move during the day. An earlier
version re-anchored at each alert, so a stock grinding steadily upward
triggered repeatedly on one move. Anchoring to yesterday's close makes "+10%"
mean the same thing all day.

Each step fires once per day per direction. Watchlist stocks are treated
identically to holdings.

---

## 3. News

Two feeds per stock — **Yahoo Finance** and **Google News**. Both are kept
because a check of their overlap found only about 44% of stories appeared in
each. Google News is searched by **company name as a quoted phrase**, not by
ticker, because "FOUR stock" and "APP stock" returned mostly unrelated
material.

### The screening, and why it was rebuilt

The original test was a substring match against ~40 keywords. It failed in
both directions, and the false positives were the worse half: content farms
write headlines stuffed with exactly those words because that is how they
rank. "5 AI stocks to watch as NVDA guidance comes into focus" matched on
`guidance` and contained no fact. It also had no notion of whether an article
was *about* the company or merely mentioned it among ten others.

`news_filter.py` screens in three stages, cheapest first:

1. **Source and shape.** Known aggregators and listicle headline shapes are
   dropped with no network call. Every pattern here came from an alert this
   bot actually sent — including single-broker price-target tweaks, which were
   six of thirteen historical false alerts, four of them the *same* AppLovin
   target moved by four different banks on one day.
2. **Dedup.** Across both feeds and against what was already sent, before any
   model call — Yahoo and Google routinely carry the same wire story.
3. **Classification.** One batched model call per run: is the company the
   primary subject, what kind of event, would a holder act on it today.

Sending requires **both** primary-subject and high impact. Either alone is
insufficient. Impact alone passes "chip stocks surge on AI demand" — real
news, about somebody else. Subject alone passes every product blog post.

Replaying the bot's own alert history through this: 11 of 13 blocked at stage
one; the other two die at stage two on subject and impact. Ten genuine events
— guidance cuts, M&A, FDA designations, a recall, a bankruptcy exit,
government contracts, a real analyst downgrade — all still get through.

Publisher names are matched in **both spaced and domain form**, because Google
News reports the outlet's own display name and for several of these it is the
domain: the history shows `simplywall.st`, not "Simply Wall St". Matching only
the prose form let that publisher through entirely.

With no model key configured, this degrades to the old keyword filter rather
than to silence.

### Not telling you twice

Three mechanisms, because duplicates arrived through three different routes:

- **Per-source IDs** — the same article isn't re-sent by the same source.
- **Fuzzy title matching** — catches the same wire story worded differently.
- **Hashed IDs** — Google's identifiers run to ~270 characters; storing short
  hashes cut `state.json` from 195KB to 24KB.

---

## 4. Earnings

Summarised here; [`EARNINGS.md`](EARNINGS.md) has the full account.

**Detection is SEC filings, not news and not IR pages.** A watch is armed for
a holding when either the Finnhub or Nasdaq calendar says it reports today or
tomorrow, or immediately when you text `earnings for TICKER`. While armed, the
watcher polls `data.sec.gov` every 15 seconds.

- **Domestic filers**: an 8-K carrying **item 2.02** ("Results of Operations
  and Financial Condition"). The SEC labels it, so nothing is inferred — never
  wrong across 166 filings.
- **Foreign filers**: 6-K has no item codes, so the exhibit text is scored by
  counting distinct financial-statement terms. Five or more plus a fiscal
  period reference means results.

You get two messages: the filing and its link on detection, then revenue, EPS,
margins, guidance and beat/miss once parsed — plus the company's own opening
paragraph verbatim, always, as a check on the machine's reading.

**Earlier approaches that failed**, each ruled out against real data rather
than assumed:

| Approach | Why it failed |
|---|---|
| Finnhub `epsActual` | Stayed empty all evening while Cerebras published minutes after the close |
| Company IR pages | Worked for 5 of 10 tested; the rest serve reCAPTCHA or render client-side |
| Keyword matching on headlines | Cerebras titled its release "Fast Inference Cloud Business Nearly Quadruples" — no results word anywhere |
| FMP press releases | HTTP 402, paid tier only |
| SEC-API.io | Would have worked, at $49/month |
| `www.sec.gov` search endpoints | 403 to Actions runners |

That last row is why this took so long to get right: `www.sec.gov/cgi-bin` and
`efts.sec.gov` both 403 from GitHub runners, and it was assumed the SEC was
blocking datacenter IPs wholesale. **`data.sec.gov` is not blocked.** Only the
search endpoints are. Finding that turned a hard problem into a simple one.

---

## 5. Portfolio tracking

You update it by texting in plain English; everything is stored in the private
repo.

**Cash is a real balance.** Buying deducts, selling credits, deposits and
withdrawals adjust it directly.

**Purchases you can't afford imply a deposit.** If a buy costs more than the
cash on hand, the shortfall is recorded as a deposit rather than letting the
balance go negative — the money had to come from somewhere for the trade to
have happened. The reply states the amount assumed.

**Money in is tracked separately** so returns mean something. Book value
ignores money added along the way, so "am I actually up?" can only be answered
against what you have actually put in. The first `set cash` seeds this with
your cash plus the cost of what you already held, since those shares predate
the bot.

**Average cost basis** throughout: buying blends the new lot into the average,
selling leaves it untouched.

---

## 6. The engineering that keeps it honest

Most problems here were **invisible failures** — things that looked fine while
silently not working. These fixes explain design choices that otherwise look
over-elaborate.

### Duplicate replies, and 163 cancelled runs

Both main workflows shared one concurrency group serving a single run at a
time. Once both ran at ~1-minute cadence, roughly two runs a minute arrived
into that lane, each taking 30–60s. The queue never drained and GitHub
cancelled 163 runs to clear it.

A cancelled Telegram run had usually already sent your reply but was killed
before recording it as handled — so the next run answered the same message
again.

The tell was subtle: those runs show as **cancelled**, not **failed**, so
filtering the run list by failures shows nothing wrong at all.

Fixed in two steps. First, separate lanes per workflow. Then, when the
listener was built, the root cause was removed: **progress is now acknowledged
to Telegram, not to git.** Requesting the next update deletes the previous one
server-side, and that happens immediately after each reply rather than at a
commit twenty seconds later. The vulnerable window went from ~20 seconds to
one HTTP round trip — which is what made `cancel-in-progress: true` safe, and
it has to be true because Telegram permits exactly one `getUpdates` connection
per bot.

### Two jobs, one state file

Separate lanes meant both could push at once, and the loser is rejected.
Rebasing cannot work — both sides rewrite every line of the same JSON.

`merge_state.py` merges the **data** instead: take the remote, re-apply only
what this run changed. Deletions are handled explicitly, because a resolved
earnings watch revived by a merge would re-send its alert.

`repo_commit.py` makes that callable from inside a running script, which the
listener needs — an hour-long loop cannot wait until exit to persist a
watchlist edit.

### The dispatch race

Arming an earnings watch dispatches the watcher, because the watcher exits
when nothing is armed. But the watcher reads `state.json` from a *fresh
checkout of main* — so dispatching before the push could start a watcher that
sees no armed watch and exits immediately, leaving the report uncovered until
the next hourly run.

Handlers now register the intent and it fires **after** the push.

### Baseline before first poll

The watcher records "everything before this filing is old" at **arm time**,
not at first poll. A company armed at 06:00 that files at 06:02 and is first
polled at 06:05 would otherwise have had its filing folded into the baseline
and treated as already seen — no alert, and a log that looked entirely normal.

### Fetch failures must not read as "nothing found"

`sec_edgar.py` raises `FetchError` rather than returning a low score when a
document can't be retrieved. VALE once scored 1 and then 12 on the same
filing; a swallowed network error looks exactly like a routine filing, which
is precisely how a missed earnings report would disguise itself.

### Telegram silently dropping alerts

An unbalanced `_` or `*` in a headline made Telegram reject the whole message
with HTTP 400, and the alert vanished without trace. External text is escaped,
failures are logged loudly, and a rejected message is retried as plain text.

### Tests

`tests/test_core_logic.py` covers what has actually broken: command parsing,
Markdown escaping, news dedup, the state merge, and the filing classifier —
including a regression test for `"2.02" in "12.02"` being true, which is what
substring-matching regulatory item codes gets you.

---

## 7. Known limitations

- **Arming is the single point of failure for earnings.** Detection is
  reliable; knowing a report is scheduled is not. Two calendars make a miss
  less likely, not impossible. `earnings for TICKER` is the override.
- **Overnight filings.** Nothing watches between 20:00 and 05:05 ET. The
  measured range is 06:00–17:23 ET, so this shouldn't bite.
- **~30–45 second handover gaps**, hourly, while a new runner provisions.
  Messages sent then aren't lost — Telegram queues updates for 24 hours.
- **GitHub's cron is best-effort** and can lag several minutes.
- **Classification is a model's judgement**, not a rule. It will occasionally
  drop something you'd have wanted, and the log is the only record.
- **Parsed figures are a machine's reading of a document.** The verbatim quote
  and the filing link are always included for exactly that reason.
- **No market-holiday calendar.** Runs happen and find nothing; harmless.
- **Nothing here is investment advice.**
