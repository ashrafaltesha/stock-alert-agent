"""Shared earnings-release detection + summary-message building + polling
loop, used by both earnings_watch.py (your own holdings) and
market_earnings_watch.py (market-wide top-cap / most-analyst-attention
reporters). Same detection method and message format either way.

Data-source: Finnhub's /calendar/earnings endpoint (requires
FINNHUB_API_KEY), queried per-symbol across a trailing window so we get both
the target release and the prior quarters needed for QoQ/YoY comparisons.
Finnhub populates epsActual/revenueActual as companies actually report, so a
release is detected as soon as Finnhub has it -- this replaced an earlier
version that checked Yahoo Finance's earnings-dates table, which could lag
hours behind the real release (e.g. AST SpaceMobile's 2026-08-10 report was
still missing from Yahoo's table well past our 3-hour poll window).

"Beat/missed/met" is based on EPS vs. Finnhub's analyst EPS estimate.
Revenue actual/QoQ/YoY also comes from Finnhub's calendar rows -- if a given
quarter's row exists but revenueActual isn't populated yet, that comparison
is just left out rather than blocking the EPS portion of the summary.
"""

import math
import time
from datetime import datetime, timedelta

from earnings_utils import (
    now_et,
    POLL_INTERVAL_SECONDS,
    POLL_TIMEOUT_MINUTES,
    fetch_earnings_history_finnhub,
)
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


def _revenue_comparison(rows: list[dict], match_pos: int) -> dict:
    """Revenue actual/QoQ/YoY from Finnhub's calendar rows, mirroring the
    EPS QoQ/YoY logic below (index+1 = prior quarter, index+4 = year ago,
    since `rows` is one row per quarter sorted newest-first)."""
    revenue_actual = _safe_float(rows[match_pos].get("revenueActual"))
    if revenue_actual is None:
        return {"revenue_pending": True}

    result = {"revenue_actual": revenue_actual}
    if match_pos + 1 < len(rows):
        prev_rev = _safe_float(rows[match_pos + 1].get("revenueActual"))
        if prev_rev:
            result["revenue_qoq_pct"] = (revenue_actual - prev_rev) / abs(prev_rev) * 100
    if match_pos + 4 < len(rows):
        prior_year_rev = _safe_float(rows[match_pos + 4].get("revenueActual"))
        if prior_year_rev:
            result["revenue_yoy_pct"] = (revenue_actual - prior_year_rev) / abs(prior_year_rev) * 100
    return result


def get_earnings_release(ticker: str, target_date_str: str) -> dict | None:
    """Returns a summary dict once the release is detected, else None."""
    target = datetime.strptime(target_date_str, "%Y-%m-%d").date()
    from_date = (target - timedelta(days=730)).strftime("%Y-%m-%d")
    rows = fetch_earnings_history_finnhub(ticker, from_date, target_date_str)
    if not rows:
        return None

    # One row per quarter -- sort newest-first so index+1/+4 line up with
    # "prior quarter" / "year ago" the same way the old yfinance version did.
    rows = sorted(rows, key=lambda r: r.get("date") or "", reverse=True)

    match_pos = None
    for i, row in enumerate(rows):
        if row.get("date") == target_date_str:
            match_pos = i
            break
    if match_pos is None:
        return None

    row = rows[match_pos]
    reported_eps = _safe_float(row.get("epsActual"))
    if reported_eps is None:
        return None  # not released yet

    eps_estimate = _safe_float(row.get("epsEstimate"))
    eps_surprise_pct = None
    if eps_estimate:
        eps_surprise_pct = (reported_eps - eps_estimate) / abs(eps_estimate) * 100

    eps_qoq_pct = None
    if match_pos + 1 < len(rows):
        prev_eps = _safe_float(rows[match_pos + 1].get("epsActual"))
        if prev_eps:
            eps_qoq_pct = (reported_eps - prev_eps) / abs(prev_eps) * 100

    eps_yoy_pct = None
    if match_pos + 4 < len(rows):
        prior_year_eps = _safe_float(rows[match_pos + 4].get("epsActual"))
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
        **_revenue_comparison(rows, match_pos),
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
        "_Beat/missed is based on EPS vs. estimate — revenue above is actual "
        "figures only, without a revenue-estimate comparison._"
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
