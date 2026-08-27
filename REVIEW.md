# Full review — efficiency, waste, and the traps to stay out of

Written 2026-08-24, after a day in which four separate failures each produced
a green Actions run. Everything below is measured against the repository as it
stands, not estimated.

Ordered by **value per unit of risk**: the top items are large, safe wins; the
bottom ones are judgement calls I would not make without you.

---

## Part 0 — The one number that matters

```
17,432 commits in the repository
17,278 of them (99.1%) are bot state commits
~1,380 per day, sustained
90.8 MiB of packed objects, dominated by state.json blobs
```

`state.json` is 44KB and is rewritten and committed roughly once a minute,
forever. This is the single largest inefficiency in the system, and it is also
the thing most likely to eventually break something: every push contends with
every other push, which is why `merge_state.py` had to exist at all.

Everything in Part 1 follows from this.

---

## Part 1 — Efficiency: where the waste actually is

### 1.1 Commit churn — HIGH value, LOW risk

**What:** `monitor.py` commits `state.json` on essentially every run because
`seen_news` grows by an article id or two each time. Nothing is alerted; the
commit exists only to remember what has been seen.

**Cost:** ~1,380 commits/day, a permanently growing pack, and constant push
contention between three workflows.

**Fix:** commit on a *reason*, not on a schedule.

- Always commit immediately when an alert was sent, a watch was armed, or a
  position changed. These must never be lost.
- Otherwise, batch: hold dedupe-only changes and commit at most every ~30
  minutes, or when the run is about to exit.

**Worst case if it goes wrong:** a runner dies holding 30 minutes of
`seen_news` and re-alerts a handful of articles. Given the news filter now
drops ~85% of candidates, that is a small, visible, self-correcting cost — as
against a repository that grows unboundedly.

**Do not:** stop persisting dedupe state, or shorten the retention window
below a day. Duplicate alerts were a real complaint earlier in this project.

### 1.2 `pip install` on every run — DONE, and it barely helped

**What was done:** `cache: pip` added to every workflow that installs.

**Measured afterwards:** the cache *does* hit --
`Cache hit for: setup-python-Linux-x64-...-pip-...` -- and the install step
still takes 12s, against 11-13s before. No measurable saving.

**Why:** `cache: pip` caches pip's DOWNLOAD cache, not the installed
environment. The 12 seconds is spent unpacking wheels into site-packages,
which happens either way. I asserted a win here before measuring one, and
then asserted the opposite ("the cache is not working") from a single run
that was populating the cache rather than restoring it. Both claims were
made ahead of the evidence.

**What would actually work,** if this is ever worth doing: cache the whole
virtualenv with `actions/cache` keyed on a hash of requirements.txt, and skip
`pip install` entirely on a hit. Saves perhaps 9s per run.

**Whether it is worth doing:** probably not. Runner minutes are free on a
public repo, and a 12-second delay is irrelevant at a 5-minute cadence. This
is cosmetic next to anything touching missed alerts.

**Note:** the earnings *watcher* already avoids this entirely by being
stdlib-only. That was the right call and is worth preserving.

### 1.3 Stale state keys — MEDIUM value, LOW risk

Six key families in `state.json` are written by code that no longer exists:

```
ir_page::              the deleted IR-page scraper
ew_amc_reminder_sent:: the deleted BMO/AMC reminder jobs
ew_poll_started::      the deleted windowed watcher
ew_summary_sent::      the deleted Finnhub summary path
ew_on_demand::         the pre-SEC on-demand watch
ew_on_demand_giveup::
```

They are dead weight, but the real cost is diagnostic: when I was reading
`state.json` during today's outage, these keys were indistinguishable from
live ones and cost time to rule out. **A one-off prune, plus a comment
recording that state keys must be removed with the code that wrote them.**

### 1.4 Four HTTP layers — MEDIUM value, MEDIUM risk

`http_utils` (requests + retry), `sec_edgar._get` (urllib + gzip + ETag),
`early_signal._fetch` (urllib), `llm_client._post` (urllib + browser headers)
— four implementations, each with its own retry, timeout and header policy.

That duplication has already cost real money twice today: the missing
User-Agent and the pinned model existed in *two* copies of the LLM code
simultaneously, and both had to be found.

**Fix:** one stdlib fetch helper with retry/backoff, used by everything that
runs inside the watcher; `http_utils` (requests) kept only for `monitor.py`,
which already depends on requests via yfinance.

**Risk:** this touches the earnings hot path. Do it with the test suite green
and do not combine it with any behaviour change.

### 1.5 `simulate.py` carries a dead backtest — LOW value, ZERO risk

`simulate.py`'s original mode replays the **Finnhub-actuals** path that SEC
detection replaced. It is the only remaining caller of
`earnings_summary.get_earnings_release`. A green backtest therefore says
nothing about the code that sends your earnings alerts — which is part of why
three broken pipeline stages went unnoticed until you ran `replay`.

**Fix:** delete the backtest mode and `earnings_summary`'s dead entry point;
keep `replay`, which exercises the real path.

---

## Part 2 — Resilience: the traps, named so we stop repeating them

Every significant failure this project has had fits one of five patterns.
These are worth reading as a checklist before any future change.

### Trap 1 — Side effect before record

**Seen four times.** The duplicate-reply bug (reply sent at 40s, offset
committed at 60s, cancelled between). The arm job (Telegram heads-up sent,
`state.json` never committed because the job cancelled itself). The watcher
dispatch race (dispatched before the state it needed was pushed). The holdings
loss (trade acknowledged, `holdings.json` committed only at job exit, which
stopped happening).

**Rule:** *persist the record, then perform the side effect.* Where that is
impossible, make the side effect idempotent.

**Test to write:** for each mutating path, assert the persistence call
precedes the outbound call. Two such assertions exist already
(`arm commits before dispatching`, `push then dispatch`); the pattern should
be applied to every new one.

### Trap 2 — Failure that looks like success

**Seen five times.** gzip bytes decoded with `errors="replace"` produced a
string, so nothing raised until JSON parsing. A swallowed `FetchError` read as
"not an earnings filing". Cancelled runs show as **cancelled**, not failed, so
filtering by failures showed nothing wrong. An unrecognised Telegram command
returned silently. A deprecated model 404 fell through to a fallback that
looked like a bad summary.

**Rule:** *every degradation must announce itself.* If a component falls back,
the output says so. If a fetch fails, the error carries the first bytes of
what came back.

**Already applied:** `FetchError` now includes the body prefix; the metrics
message says when extraction failed; unmatched commands reply.

**Still missing:** nothing tells you when the *whole chain* stops. See 3.1.

**The inverse, found 2026-08-27: success that looks like failure.** The
watcher records a heartbeat every 15-second cycle, but `earnings_watch.yml`
commits `state.json` in one step *after* the loop — deliberately, since a git
push inside the poll costs more than the poll interval. So the recorded
timestamp measures time since the last run *ended*. Against a 90-minute
staleness limit and a 330-minute loop, a perfectly healthy watcher read as
broken for four hours out of every five and a half. I used that number to
judge a live run and nearly declared it dead.

**Rule:** *a value persisted only at exit cannot measure liveness.* Ask the
platform that owns the process; keep the timestamp as the fallback for when
it cannot be reached. `health.EXIT_PERSISTED` names the components this
applies to, and a test asserts their fallback limit can never drift below
the loop length it is measuring.

The corollary is what `status` now reports: not "the watcher is quiet", but
"a watch is armed and nothing is polling" — which is the condition that
actually costs an alert. Idle with nothing armed is correct on the ~215
trading days a year with nothing to watch.

### Trap 3 — Pinned external assumption

**Seen three times.** `llama-3.3-70b-versatile` was retired by Groq.
`www.sec.gov` was assumed blocked when only its search endpoints are.
`acceptanceDateTime` was assumed UTC when EDGAR writes Eastern.

**Rule:** *prefer lists and discovery to constants, and verify assumptions
against the provider rather than against memory.* The model list now falls
through and can query `/v1/models`; the same treatment is worth considering
for the Gemini model names.

### Trap 4 — Untrusted text in a control structure

**Seen twice, same root.** `PROMPT % (ticker, release_text)` crashed on
"Revenue increased 12%, driven by…". The same bug existed in `news_filter`
with headlines. Earlier, unbalanced Markdown in a headline made Telegram
reject an entire alert.

**Rule:** *never let fetched text participate in formatting or markup.*
Substitute placeholders; escape before sending.

### Trap 5 — Assuming the platform is punctual or durable

**Seen twice.** The "hourly" listener cron fired at 19:56, 21:03, 23:33,
01:58, 04:29, 06:58, 10:26 — gaps up to 89 minutes. And a long-running job
runs the code it checked out, so a fix pushed at 15:45 was invisible until
21:10.

**Rule:** *never let coverage depend on a scheduled event arriving.* The long
loop plus the five-minute watchdog plus the code-change self-restart are the
current answer, and they are sound.

---

## Part 3 — Gaps still open

### 3.1 Nothing reports that the system has stopped — HIGHEST remaining risk

Today every component reported success while four things were broken. You
found each one because *you* were expecting something. That is the only
detector currently in place.

**Fix:** a dead-man's switch. Each workflow pings a URL on success;
[healthchecks.io](https://healthchecks.io) is free for 20 checks and emails
when a ping is missed. One `curl` line per workflow.

This is the highest-value item in this document. It does not make anything
faster; it makes every other failure *visible within minutes*, which today
would have been worth more than all the performance work combined.

### 3.2 Arming still depends on two calendars

Detection is solid. Knowing a report is scheduled is not. If neither Finnhub
nor Nasdaq lists a company, nothing arms and the whole chain is idle. The
`ew_mark::` catch-up now recovers this *after the fact*, on the next arm,
which is a genuine improvement — but "the next arm" may be the following
morning.

**Option:** arm a watch for any holding whose last earnings filing is more
than ~80 days old, regardless of calendar. Cheap, self-maintaining, and
catches exactly the recent-IPO and foreign-issuer cases the calendars miss.

### 3.3 The SEC lag for dual-listed issuers is real

XPeng announced Q2 on 2026-08-24 and had not filed the 6-K hours later. The
wire-headline early signal covers this, but it is new and unproven — it has
never fired live. Worth watching on the next foreign issuer that reports.

### 3.4 No health command

There is no way to ask the bot whether it is working. A `status` command
returning listener uptime, armed watches, last alert sent, and last successful
SEC poll would have answered, in one message, several questions that took
Actions-log archaeology today.

---

## Part 4 — What NOT to change

Recorded because each of these looks like an obvious efficiency and is not.

- **The stdlib-only rule in the earnings watcher.** Saves 25-55s of startup on
  the one path where latency is the product.
- **Two news sources.** Their overlap was measured at ~44%; each catches what
  the other misses.
- **Two earnings calendars, unioned.** A wasted watch costs a few hundred
  requests; a missed one costs the alert.
- **The verbatim release quote alongside parsed figures.** It is the only
  check on a model's reading of a document.
- **`merge_state.py` instead of rebasing.** A rebase cannot succeed when both
  sides rewrite every line, and when it fails it drops data silently.
- **`cancel-in-progress: true` on the listener.** Telegram permits exactly one
  `getUpdates` connection; queueing would guarantee two pollers.

---

## Suggested order

| # | Item | Effort | Payoff |
|---|---|---|---|
| 1 | Dead-man's switch (3.1) | 30 min | Every future failure becomes visible in minutes |
| 2 | `cache: pip` (1.2) | 5 min | ~1 hour of runner time per day |
| 3 | Prune stale state keys (1.3) | 15 min | Faster diagnosis, smaller file |
| 4 | Commit on a reason (1.1) | 1-2 h | Ends 1,380 commits/day |
| 5 | `status` command (3.4) | 45 min | Answers "is it working" without logs |
| 6 | Delete the dead backtest (1.5) | 20 min | Removes a test that lies |
| 7 | Consolidate HTTP layers (1.4) | 2-3 h | One place to fix provider quirks |
| 8 | Calendar-independent arming (3.2) | 1-2 h | Closes the last big earnings gap |

Items 1-3 are safe to do in any order and touch nothing that runs in a hot
path. Item 4 changes durability semantics and deserves its own commit and its
own test. Item 7 touches the earnings path and should be done alone, with the
suite green before and after.
