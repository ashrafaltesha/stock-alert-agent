"""One-off backtest/simulation tool: replays a past trading day's price
action and earnings releases through the SAME detection logic as
monitor.py, earnings_watch.py, and market_earnings_watch.py, sending real
Telegram messages (clearly prefixed "SIMULATION") so you can verify the
alert pipeline and formatting end-to-end without waiting for it to happen
live.

Does NOT read or write state.json, so it can never interfere with real
alert dedup state or the live bot's memory of what it has already sent.

Usage:
  python simulate.py [YYYY-MM-DD]           backtest a past trading day
  python simulate.py replay TICKER [ACCN]   replay one real SEC filing

The `replay` mode exists because the backtest above does NOT exercise the
current earnings pipeline. It replays the Finnhub-actuals path that SEC
filing detection replaced, so a green backtest says nothing about whether
the code that now sends your earnings alerts works.

`replay` runs the real thing end to end -- fetch the filing from EDGAR,
classify it, extract the figures, compare to consensus, format the message
-- against a filing that genuinely exists. It is how you answer "would this
have worked?" after a miss, and how you check a change to the extraction
or formatting without waiting for a company to report.

Triggered manually via .github/workflows/simulate.yml (workflow_dispatch
only -- never runs on a schedule).
"""

import json
import os
import sys
from datetime import datetime, timedelta

import yfinance as yf

from config import PRICE_CHANGE_THRESHOLD_PCT
from telegram_utils import send_telegram_message
from earnings_utils import classify_holdings_for_date
from earnings_summary import get_earnings_release, build_summary_message
from market_earnings_watch import select_top_reporters, format_list_line

SIM_TAG = "\U0001F9EA *SIMULATION*"


def load_tickers() -> list[str]:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tickers.json")
    with open(path) as f:
        return json.load(f)


def simulate_price_alerts(tickers: list[str], sim_date: str) -> None:
    target = datetime.strptime(sim_date, "%Y-%m-%d").date()
    next_day = (target + timedelta(days=1)).strftime("%Y-%m-%d")
    lookback_start = (target - timedelta(days=10)).strftime("%Y-%m-%d")
    threshold = PRICE_CHANGE_THRESHOLD_PCT / 100

    for ticker in tickers:
        try:
            daily = yf.Ticker(ticker).history(start=lookback_start, end=next_day, interval="1d")
        except Exception as e:
            print(f"[{ticker}] daily history failed: {e}")
            continue
        if daily.empty:
            print(f"[{ticker}] no daily history returned")
            continue

        prior_days = daily[daily.index.date < target]
        if prior_days.empty:
            print(f"[{ticker}] no prior close found before {sim_date}")
            continue
        prev_close = float(prior_days["Close"].iloc[-1])

        try:
            intraday = yf.Ticker(ticker).history(start=sim_date, end=next_day, interval="5m")
        except Exception as e:
            print(f"[{ticker}] intraday history failed: {e}")
            continue
        if intraday.empty:
            print(f"[{ticker}] no intraday bars for {sim_date} (holiday / no data?)")
            continue

        reference_price = prev_close
        moves_today = 0

        for ts, row in intraday.iterrows():
            price = float(row["Close"])
            while True:
                pct_from_ref = (price - reference_price) / reference_price * 100
                if abs(pct_from_ref) < PRICE_CHANGE_THRESHOLD_PCT:
                    break
                direction_up = pct_from_ref > 0
                new_reference = reference_price * (1 + threshold if direction_up else 1 - threshold)
                moves_today += 1
                pct_from_close = (new_reference - prev_close) / prev_close * 100
                emoji = "\U0001F4C8" if direction_up else "\U0001F4C9"
                try:
                    time_label = ts.strftime("%-I:%M %p ET")
                except Exception:
                    time_label = str(ts)
                msg = (
                    f"{SIM_TAG} ({sim_date})\n"
                    f"{emoji} *{ticker}* move #{moves_today} that day: "
                    f"{'up' if direction_up else 'down'} {PRICE_CHANGE_THRESHOLD_PCT:.1f}% "
                    f"from its last checkpoint\n"
                    f"At {time_label}: ${price:.2f}  |  {pct_from_close:+.1f}% vs prev close (${prev_close:.2f})"
                )
                send_telegram_message(msg)
                reference_price = new_reference

        if moves_today == 0:
            print(f"[{ticker}] no threshold moves on {sim_date} (prev close ${prev_close:.2f})")


def simulate_holdings_earnings(tickers: list[str], sim_date: str) -> None:
    classification = classify_holdings_for_date(sim_date, tickers)
    if not classification:
        print(f"No holdings reporting earnings on {sim_date}.")
        return

    for ticker, cat in classification.items():
        send_telegram_message(
            f"{SIM_TAG} ({sim_date})\n"
            f"\U0001F514 *{ticker}* reported earnings {cat.upper()} on {sim_date}."
        )
        data = get_earnings_release(ticker, sim_date)
        if data:
            send_telegram_message(f"{SIM_TAG} ({sim_date})\n" + build_summary_message(ticker, sim_date, data))
        else:
            send_telegram_message(
                f"{SIM_TAG} ({sim_date})\n"
                f"⚠️ *{ticker}*: earnings data not found/detected for {sim_date}."
            )


def simulate_market_earnings(sim_date: str) -> None:
    top_cap, top_analyst = select_top_reporters(sim_date)
    if not top_cap and not top_analyst:
        print(f"No market-wide earnings calendar data for {sim_date}.")
        return

    lines = [f"{SIM_TAG} ({sim_date})", f"\U0001F4CB *Top {len(top_cap)} market-wide earnings* ({sim_date}):"]
    lines += [format_list_line(r) for r in top_cap] if top_cap else ["None found."]
    send_telegram_message("\n".join(lines))

    lines2 = [f"{SIM_TAG} ({sim_date})", "\U0001F4CB *Most analyst attention*:"]
    lines2 += [format_list_line(r) for r in top_analyst] if top_analyst else ["None found."]
    send_telegram_message("\n".join(lines2))

    watch_symbols = sorted({r.get("symbol") for r in (top_cap + top_analyst) if r.get("symbol")})
    for symbol in watch_symbols:
        data = get_earnings_release(symbol, sim_date)
        if data:
            send_telegram_message(f"{SIM_TAG} ({sim_date})\n" + build_summary_message(symbol, sim_date, data))
        else:
            print(f"[{symbol}] no earnings release detected for {sim_date}")


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
        for f in filings[:15]:
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
    if len(sys.argv) > 1 and sys.argv[1] == "replay":
        if len(sys.argv) < 3:
            print("Usage: python simulate.py replay TICKER [ACCESSION]")
            return
        replay_filing(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
        return

    sim_date = sys.argv[1] if len(sys.argv) > 1 else "2026-08-07"
    tickers = load_tickers()

    send_telegram_message(
        f"{SIM_TAG}\nStarting a backtest for *{sim_date}* -- replaying price-threshold "
        f"alerts and earnings reports against that day's real market data. These "
        f"messages are test-only and don't reflect live alerts or change any saved state."
    )

    simulate_price_alerts(tickers, sim_date)
    simulate_holdings_earnings(tickers, sim_date)
    simulate_market_earnings(sim_date)

    send_telegram_message(f"{SIM_TAG}\nBacktest for *{sim_date}* complete.")


if __name__ == "__main__":
    main()
