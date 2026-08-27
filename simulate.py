"""Replays a REAL SEC filing through the live earnings pipeline.

Why this file exists at all
---------------------------
It used to be a backtester: it replayed a past trading day's price action and
earnings through what were then the detection paths, sending Telegram messages
prefixed "SIMULATION".

The earnings half of that became a liability. It exercised
earnings_summary.get_earnings_release() -- the Finnhub-actuals path that SEC
filing detection replaced -- so a clean backtest said nothing about the code
that actually sends earnings alerts. On 2026-08-24 that pipeline was broken in
three places at once (gzip never decoded, prompts crashing on percent signs, a
deprecated model) and every existing check still passed. The first thing to
catch any of it was running `replay` against a real filing.

So the backtest is gone and this is only the replay. A test that cannot fail
when the system is broken is worse than no test, because it is quoted as
evidence.

Usage:
  python simulate.py replay TICKER [ACCESSION]

Runs the live path end to end -- fetch from EDGAR, classify, extract figures,
compare to consensus, format, send -- against a filing that genuinely exists.
It is how to answer "would this have worked?" after a miss, and how to check a
change to extraction or formatting without waiting for a company to report.

Reads and writes NOTHING in state.json, deliberately. A replay must not be
able to mark a filing as seen and thereby suppress the real alert for it.

Triggered manually via .github/workflows/simulate.yml (workflow_dispatch
only -- never on a schedule).
"""

import sys
from datetime import datetime

from telegram_utils import send_telegram_message

SIM_TAG = "\U0001F9EA *SIMULATION*"


def replay_filing(ticker: str, accession: str = "") -> None:
    """Run one real SEC filing through the live detection and alert path.

    Reads nothing from state.json and writes nothing to it, so replaying a
    filing cannot mark it as seen and cannot suppress the real alert if the
    watcher is about to find it independently.
    """
    import sec_edgar
    import earnings_watch
    from earnings_utils import fetch_consensus

    ticker = ticker.upper()
    cik = sec_edgar.resolve_cik(ticker, sec_edgar.load_cik_map())
    if not cik:
        send_telegram_message(f"{SIM_TAG}\nNo CIK known for *{ticker}*.")
        print(f"No CIK for {ticker}; add it to cik_map.json.")
        return

    filings, _ = sec_edgar.recent_filings(cik)
    if not filings:
        print(f"No filings returned for {ticker}.")
        return

    if accession:
        match = [f for f in filings if f["accession"] == accession]
        if not match:
            print(f"{accession} not in {ticker}'s recent filings.")
            print("Recent: " + ", ".join(f["accession"] for f in filings[:8]))
            return
        target = match[0]
    else:
        # Newest filing that classifies as results, so the common case is
        # just `replay TICKER`.
        target = None
        # Candidates, not raw rows. Replaying RBRK on 2026-08-27 reported "no
        # recent filing classifies as earnings" while its 8-K sat on EDGAR at
        # index 28, behind a wall of Form 4s and 144s.
        for f in sec_edgar.earnings_candidates(filings, 15):
            if sec_edgar.is_domestic_earnings(f):
                target = f
                break
            if f["form"] == "6-K":
                try:
                    score, period, _ = sec_edgar.score_filing(cik, f["accession"])
                except sec_edgar.FetchError as e:
                    print(f"scoring {f['accession']} failed: {e}")
                    continue
                if sec_edgar.is_foreign_earnings(score, period):
                    target = f
                    break
        if target is None:
            print(f"No recent {ticker} filing classifies as earnings.")
            send_telegram_message(
                f"{SIM_TAG}\nNo recent *{ticker}* filing classifies as earnings. "
                f"If they have announced results, the 6-K has not reached EDGAR yet."
            )
            return

    print(f"Replaying {ticker} {target['form']} {target['accession']} "
          f"accepted {target.get('accepted')}")

    # Say so loudly when the newest earnings filing is not recent. Picking
    # the newest match is convenient, but silently replaying LAST quarter
    # while you are looking for today's is worse than refusing: the message
    # arrives, the numbers look plausible, and nothing says they are stale.
    filed = (target.get("filed") or "")
    if filed and (datetime.now().date() - datetime.strptime(filed, "%Y-%m-%d").date()).days > 3:
        stale = (f"{SIM_TAG}\n\u26A0\uFE0F Newest *{ticker}* earnings filing on EDGAR "
                 f"is from *{filed}*, not today. Replaying that one. If results "
                 f"were announced today, the 6-K has not been filed yet.")
        print(stale.replace(SIM_TAG, "").strip())
        send_telegram_message(stale)

    score, period, text = sec_edgar.score_filing(cik, target["accession"])
    kind = ("8-K item 2.02" if sec_edgar.is_domestic_earnings(target)
            else f"6-K (score {score}, period={period})")
    verdict = (sec_edgar.is_domestic_earnings(target)
               or sec_edgar.is_foreign_earnings(score, period))
    print(f"classified as earnings: {verdict}  ({kind})")

    consensus = fetch_consensus(ticker, (target.get("filed") or "")) or {}
    if consensus:
        print(f"consensus: {consensus}")

    send_telegram_message(
        f"{SIM_TAG}\n\U0001F4CA *{ticker} earnings filing (replay)*\n"
        f"{kind}, accepted {target.get('accepted')}\n"
        f"{sec_edgar.filing_url(cik, target['accession'], target.get('doc'))}"
    )

    message = earnings_watch.build_metrics_message(ticker, text, consensus)
    if message:
        send_telegram_message(f"{SIM_TAG}\n{message}")
    else:
        send_telegram_message(
            f"{SIM_TAG}\nNothing could be extracted from the document. "
            f"With no GROQ_API_KEY or GEMINI_API_KEY set this is expected."
        )
    print("Replay complete.")


def main() -> None:
    if len(sys.argv) < 3 or sys.argv[1] != "replay":
        print("Usage: python simulate.py replay TICKER [ACCESSION]")
        return
    replay_filing(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")


if __name__ == "__main__":
    main()
