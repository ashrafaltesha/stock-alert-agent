"""Shared earnings-release detection + summary-message building + polling
loop, used by both earnings_watch.py (your own holdings) and
market_earnings_watch.py (market-wide top-cap / most-analyst-attention
reporters). Same detection method and message format either way.

Data-source caveats (worth knowing):
  - "Beat/missed/met" is based on EPS vs. the analyst EPS estimate (from
    Yahoo Finance), since a free, reliable analyst *revenue* estimate isn't
    available. Revenue is still reported as an actual dollar figure with
    QoQ/YoY change, just without an estimate to compare against.
  - Revenue QoQ/YoY comes from Yahoo's quarterly income statement, which can
    lag a few hours to a couple of days behind the initial EPS release --
    if it hasn't updated yet, the summary says so and still sends the EPS
    portion immediately rather than waiting.
"""

import math
import time
from datetime import datetime, timedelta

import yfinance as yf

from earnings_utils import now_et, POLL_INTERVAL_SECONDS, POLL_TIMEOUT_MINUTES
from telegram_utils import send_telegram_message
from state_utils import save_state


def _safe_float(x):
    try:
        if x is None:
            return None
        f = float(x)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def get_revenue_comparison(ticker: str, target_date) -> dict:
    try:
        qtr = yf.Ticker(ticker).quarterly_income_stmt
    except Exception as e:
        print(f"[{ticker}] quarterly_income_stmt failed: {e}")
        return {}
    if qtr is None or qtr.empty or "Total Revenue" not in qtr.index:
        return {}

    revenue_row = qtr.loc["Total Revenue"].dropna()
    if revenue_row.empty:
        return {}

    cols_sorted = sorted(revenue_row.index, reverse=True)
    latest_col = cols_sorted[0]
    latest_col_date = latest_col.date() if hasattr(latest_col, "date") else latest_col

    # If the most recent column in the statement isn't close to (and not
    # after) the earnings date, the statement likely hasn't refreshed for
    # this release yet -- flag as pending rather than showing stale data.
    if latest_col_date > target_date or (target_date - latest_col_date).days > 100:
        return {"revenue_pending": True}

    latest_revenue = float(revenue_row[latest_col])
    result = {"revenue_actual": latest_revenue}

    if len(cols_sorted) >= 2:
        prev_q = float(revenue_row[cols_sorted[1]])
        if prev_q:
            result["revenue_qoq_pct"] = (latest_revenue - prev_q) / abs(prev_q) * 100
    if len(cols_sorted) >= 5:
        prev_y = float(revenue_row[cols_sorted[4]])
        if prev_y:
            result["revenue_yoy_pct"] = (latest_revenue - prev_y) / abs(prev_y) * 100

    return result


def get_earnings_release(ticker: str, target_date_str: str) -> dict | None:
    """Returns a summary dict once the release is detected, else None."""
    try:
        dates_df = yf.Ticker(ticker).get_earnings_dates(limit=12)
    except Exception as e:
        print(f"[{ticker}] get_earnings_dates failed: {e}")
        return None
    if dates_df is None or dates_df.empty:
        return None

    dates_df = dates_df.sort_index(ascending=False)
    target = datetime.strptime(target_date_str, "%Y-%m-%d").date()
    idx_list = list(dates_df.index)

    match_pos = None
    for i, idx in enumerate(idx_list):
        idx_date = idx.date() if hasattr(idx, "date") else idx
        if idx_date == target:
            match_pos = i
            break
    if match_pos is None:
        return None

    row = dates_df.iloc[match_pos]
    reported_eps = _safe_float(row.get("Reported EPS"))
    if reported_eps is None:
        return None  # not released yet

    eps_estimate = _safe_float(row.get("EPS Estimate"))
    eps_surprise_pct = _safe_float(row.get("Surprise(%)"))

    eps_qoq_pct = None
    if match_pos + 1 < len(idx_list):
        prev_eps = _safe_float(dates_df.iloc[match_pos + 1].get("Reported EPS"))
        if prev_eps:
            eps_qoq_pct = (reported_eps - prev_eps) / abs(prev_eps) * 100

    eps_yoy_pct = None
    if match_pos + 4 < len(idx_list):
        prior_year_eps = _safe_float(dates_df.iloc[match_pos + 4].get("Reported EPS"))
        if prior_year_eps:
            eps_yoy_pct = (reported_eps - prior_year_eps) / abs(prior_year_eps) * 100

    beat_miss = None
    if eps_estimate is not None:
        if reported_eps > eps_estimate:
            beat_miss = "beat"
        elif reported_eps < eps_estimate:
            beat_miss = "missed"
        else:
            beat_miss = "met"

    return {
        "reported_eps": reported_eps,
        "eps_estimate": eps_estimate,
        "eps_surprise_pct": eps_surprise_pct,
        "eps_qoq_pct": eps_qoq_pct,
        "eps_yoy_pct": eps_yoy_pct,
        "beat_miss": beat_miss,
        **get_revenue_comparison(ticker, target),
    }


def format_money(x) -> str:
    if x is None:
        return "n/a"
    abs_x = abs(x)
    if abs_x >= 1e9:
        return f"${x / 1e9:.2f}B"
    if abs_x >= 1e6:
        return f"${x / 1e6:.1f}M"
    return f"${x:,.0f}"


def format_pct(x) -> str:
    return "n/a" if x is None else f"{x:+.1f}%"


def build_summary_message(ticker: str, target_date: str, data: dict) -> str:
    beat_miss = data.get("beat_miss")
    emoji = {"beat": "✅", "missed": "❌", "met": "➖"}.get(beat_miss, "\U0001F4CA")
    label = {"beat": "BEAT", "missed": "MISSED", "met": "MET"}.get(beat_miss, "reported vs.")

    eps_est_str = f"${data['eps_estimate']:.2f}" if data.get("eps_estimate") is not None else "n/a"
    lines = [
        f"{emoji} *{ticker} earnings released* ({target_date})",
        f"EPS: ${data['reported_eps']:.2f} vs. est. {eps_est_str} — {label} expectations",
        f"EPS vs last quarter: {format_pct(data.get('eps_qoq_pct'))}  |  vs year ago: {format_pct(data.get('eps_yoy_pct'))}",
    ]

    if data.get("revenue_pending"):
        lines.append("Revenue: not yet reflected in data source — check back shortly")
    elif data.get("revenue_actual") is not None:
        lines.append(f"Revenue: {format_money(data['revenue_actual'])}")
        lines.append(
            f"Revenue vs last quarter: {format_pct(data.get('revenue_qoq_pct'))}  |  "
            f"vs year ago: {format_pct(data.get('revenue_yoy_pct'))}"
        )
    else:
        lines.append("Revenue: unavailable")

    lines.append(
        "_Beat/missed is based on EPS vs. estimate — a free revenue-estimate "
        "source isn't reliably available, so revenue above is actual figures only._"
    )
    return "\n".join(lines)


def poll_for_releases(tickers: list[str], target_date: str, state: dict) -> None:
    """Checks each ticker roughly once a minute until its earnings release is
    detected (sending a summary immediately), or until POLL_TIMEOUT_MINUTES
    elapses (sending one give-up notice per still-pending ticker). Mutates
    and persists `state` as it goes, so progress survives an interrupted job."""
    deadline = now_et() + timedelta(minutes=POLL_TIMEOUT_MINUTES)
    pending = set(tickers)

    while pending:
        for ticker in list(pending):
            data = get_earnings_release(ticker, target_date)
            if data:
                send_telegram_message(build_summary_message(ticker, target_date, data))
                state[f"ew_summary_sent::{ticker}::{target_date}"] = True
                pending.discard(ticker)
                save_state(state)

        if not pending or now_et() >= deadline:
            break
        time.sleep(POLL_INTERVAL_SECONDS)

    for ticker in pending:
        giveup_key = f"ew_poll_giveup::{ticker}::{target_date}"
        if not state.get(giveup_key):
            send_telegram_message(
                f"⚠️ *{ticker}*: earnings still not detected as released after "
                f"~{POLL_TIMEOUT_MINUTES} min of checking. It may be delayed — worth a manual look."
            )
            state[giveup_key] = True
    save_state(state)
