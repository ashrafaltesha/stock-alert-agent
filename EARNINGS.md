# How earnings detection works

Two ways a company ends up being watched: **automatically, because you hold
it**, or **manually, because you asked**. They differ only in what creates the
watch. Once one exists, detection and delivery are identical.

---

## Part 1 — Your holdings (automatic)

### Step 1: Arming

Twice a day — **05:00 and 15:00 ET** (04:00 and 14:00 in winter; 09:00 and
19:00 UTC) — a small job asks two calendars which of your holdings report
**today or tomorrow**:

Weekdays only. Nothing arms over the weekend, so a Monday pre-market reporter
is armed by Monday's 05:00 ET run — before the earliest filing time measured,
but with no margin to spare. If you know about it in advance, text
`earnings for TICKER` the night before.

| Source | Notes |
|---|---|
| Finnhub | needs an API key, already configured |
| Nasdaq | no key, public endpoint |

**Either one listing a company is enough.** The results are unioned, not
intersected, because the two failure costs aren't comparable: a wasted watch
burns a few hundred HTTP requests and expires quietly after 24 hours, while a
missed one costs the alert entirely.

If one source is down, the other still arms. If both are down, nothing is
armed — that's the gap, and the manual command is the override.

You get a heads-up per company:

> 🔔 **CBRS** reports earnings tomorrow. I'm watching their SEC filings and
> will send results within seconds of them being filed.

### Step 2: The baseline

At the moment of arming, the bot records the company's **most recent SEC
filing accession number**. That's the "everything before this is old" marker.

This is recorded at arm time rather than at first poll, and the distinction
matters: a company armed at 06:00 that files at 06:02, first polled at 06:05,
would otherwise have had its filing folded into the baseline and treated as
already seen. No alert, and a log that looked entirely normal.

### Step 3: Polling

A watcher job runs **hourly from 05:05 to 19:05 ET on weekdays** (09:05–23:00
UTC), each looping for 62 minutes so the incoming run overlaps the outgoing
one. It starts an hour before the earliest filing ever measured (SAP 06:00 ET;
TSMC and Honda 06:02) and runs past the latest (Novo 17:23).

While a watch is armed, it checks that company's SEC filings **every 15
seconds**. With nothing armed it exits within about 20 seconds rather than
idling — on a normal day that saves roughly eleven hours of pointless runner
time.

Because it exits when idle, **arming starts a watcher**. Both the calendar job
and the Telegram command do this.

### Step 4: Recognising the release

**Domestic filers — 8-K with item 2.02.**
Item 2.02 is "Results of Operations and Financial Condition". The SEC labels
the earnings release itself, so nothing is inferred. Across 166 filings from
15 companies this never once mislabelled anything.

**Foreign filers — 6-K, scored on content.**
6-K has no item codes, so the filing's own text decides. The bot fetches the
exhibits and counts financial-statement terms — revenue, net income, per
share, gross margin, EBITDA, and so on. **Five or more, plus a reference to a
fiscal period, means results.**

Every cheaper signal was tested against 40 foreign issuers and every one
failed:

| Signal | Why it failed |
|---|---|
| XBRL flags | 0 for all 344 of Alibaba's 6-Ks |
| `reportDate` | just mirrors the filing date |
| File size | Alibaba's earnings exhibit 22KB, a routine one 10KB |
| Filename | Genius Sports names them `q2_26`; XPeng names everything `dNNNNNNd6k.htm` |
| Attachment type | the index field is a UI icon, not a category |

The threshold is 5 rather than 7 because **Honda scores 5–6 every single
quarter**. A smaller sample suggested 7 was safe; Honda alone would have been
missed four times a year.

Precision holds up: HSBC files daily buyback notices and produced exactly
**one hit in 45 filings**. Li Auto, one in 26.

### Step 5: Delivery

Two messages, deliberately.

**Immediately on detection** — the fact, before anything is parsed:

> 📊 **AMAT earnings just filed**
> 8-K item 2.02, accepted 2026-08-13T20:03:36Z
> https://www.sec.gov/Archives/edgar/data/6951/...

**A few seconds later**, once the release has been read:

> **EPS $2.48 (adj.) vs 2.41 expected — beat by 0.07**
> Revenue rose 8% on record foundry demand.
>
> Period: Q3 2026
> Revenue: $7.31 billion
> Revenue YoY: +8%
> EPS (adj.): $2.48
> EPS (GAAP): $2.30
> Gross margin: 48.1%
> Operating income: $2.10 billion
> Free cash flow: $1.42 billion
>
> **Guidance**: Q4 revenue of $7.4B ± $400M.
>
> _From the release:_
> Applied Materials today reported results for its third quarter...

The split exists so you learn results are out at the earliest possible moment
rather than waiting on parsing.

**Order is deliberate.** Beat/miss leads because it is the most price-relevant
single line. Guidance follows, because it frequently moves the stock more than
the quarter itself. The company's own words come last.

**Consensus is fetched at arm time, not at alert time.** Finnhub's `epsActual`
lags badly — it stayed empty all evening while Cerebras published minutes
after the close, which is what started this whole rebuild — but `epsEstimate`
is set *before* the company reports and doesn't lag at all. Storing it up
front also means the beat/miss line costs nothing when latency matters.

Only **EPS** is compared to consensus, never revenue. Revenue estimates and
reported revenue are quoted on different bases often enough
(constant-currency, segment splits, net vs gross) that a mechanical comparison
would mislead more than it informs.

**Extraction uses a model, and the verbatim quote is always included anyway.**
An earnings release states the same metric several times over — current
quarter, year-ago quarter, six months ended, GAAP beside non-GAAP, and again
inside HTML tables. A regex like `revenue of \$X` frequently matches the
*year-ago* figure with no signal that it did, and a confidently wrong revenue
number is worse than none. A model reads the surrounding sentence and gets the
period and the GAAP/non-GAAP distinction right. The quote is what the company
actually wrote, and it costs a few lines to make the difference between the
two visible rather than invisible.

With no `GROQ_API_KEY` or `GEMINI_API_KEY` set, the first message is unchanged
and the second falls back to the quote alone.

### Step 6: Expiry

A watch lasts 24 hours. If nothing was filed, you get one message saying so.
If something was, it exits quietly — a "nothing found" note after sending you
three filings would be plainly wrong.

---

## Part 2 — Custom requests (`earnings for <ticker>`)

### What's different

Only the trigger. You text the bot; it arms a watch immediately.

**Any ticker works** — held, watchlisted, or neither. The only check is that
live market data exists for the symbol, which catches typos.

**No calendar can veto it.** Finnhub is consulted for context and will note
that a ticker isn't listed as reporting today, but the watch is armed anyway.
This is the override for exactly the case the calendars get wrong: recent IPOs
and foreign issuers.

You get:

> 🔔 Watching NVDA for the next 24h. I check their SEC filings every 15
> seconds and will send results within seconds of them being filed.

Sending the command also **starts a watcher**, so polling begins within
seconds rather than waiting for the next hourly run.

The dispatch happens **after** the new watch is committed and pushed, not
before. The watcher reads `state.json` from a fresh checkout of `main`, so
dispatching first was a race it could lose: the watcher would start, see no
armed watch, exit — and the report would go uncovered until the next hourly
run, or until the next morning outside watch hours.

### What's identical

Steps 3 through 6 above — same polling, same 8-K item 2.02 and 6-K content
rules, same two-stage delivery, same 24-hour expiry.

### When to use it

- Ahead of a report you're specifically waiting on, in case both calendars
  miss it
- For a company you don't own
- For a recent IPO, where calendar coverage is weakest

---

## Side by side

| | Holdings | `earnings for <ticker>` |
|---|---|---|
| What arms it | Finnhub **or** Nasdaq calendar | your message |
| When | 05:00 and 15:00 ET, for today/tomorrow | immediately |
| Coverage | only what a calendar lists | anything you name |
| Baseline set | at arm time | at first poll (~15s later) |
| Watcher started | by the arm job | by the Telegram listener, *after* the state push |
| Detection | identical | identical |
| Delivery | identical | identical |
| Expiry | 24h | 24h |

---

## Timing

| Stage | Time |
|---|---|
| SEC accepts the filing → visible in their API | **unmeasured** |
| Waiting for the next poll | ≤15s |
| Fetch and classify | 1–3s |
| Telegram delivery | <1s |

**Expected: roughly 15–20 seconds after the filing appears in the SEC's API.**
Down from 90–150 seconds under the old design, almost all of which was GitHub
spending 45–100 seconds provisioning a machine and installing packages to do
two seconds of work.

The unmeasured line matters. Nobody has established how long the SEC takes to
publish a filing into its API after accepting it. If that turns out to be 45
seconds, then polling every 15 gains nothing below a minute. Every detection
logs `DETECTION LAG Ns after SEC acceptance` — the first real filing will
answer it.

---

## Known gaps

**Neither calendar lists the company.** No watch, no detection. Two sources
make this less likely than one, but they can both miss the same recent IPO.
`earnings for <ticker>` is the override.

**Overnight filings.** Nothing runs between 20:00 and 05:05 ET. The measured
range is 06:00–17:23, so this shouldn't bite, but a 04:00 filing would wait
until 05:05.

**~30-second handover gap.** Once an hour, while a new runner provisions.
Unavoidable on GitHub Actions; only a process that never restarts closes it.

**Bare-cover foreign filings.** Sea Limited files 1,400-character 6-K covers
with no exhibit. There is nothing to read, so no content rule can catch it.

**Figures are a model's reading.** Extraction is careful and the prompt is
strict about current-period and GAAP/non-GAAP, but it is still a machine
reading a document. The verbatim quote and the filing link are in every alert
for exactly that reason — check the figure against them before acting on it.

**No model key means no figures.** The detection alert still arrives; the
follow-up carries the company's opening paragraph instead of parsed metrics.
