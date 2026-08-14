"""Targeted tests for the logic that has actually broken before.

Deliberately narrow. These cover pure functions with no network calls, so
the suite runs in about a second and needs no secrets:

  * the Telegram command regexes (a watchlist/list collision was a real
    bug once -- "add X to my watchlist" must not be caught by the "add X
    to my list" pattern)
  * Markdown escaping of external text (unbalanced characters in a news
    headline used to make Telegram reject the whole message)
  * news-id hashing and the one-time migration of old raw ids
  * the material-news keyword filter

Run locally with:  python -m pytest tests/ -q
"""

import os
from datetime import datetime
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The config module reads these at import time; tests never send anything.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "test-chat")
os.environ.setdefault("FINNHUB_API_KEY", "test-key")

import ir_feeds  # noqa: E402
import merge_state  # noqa: E402
import monitor  # noqa: E402
import telegram_commands as tc  # noqa: E402
from telegram_utils import escape_markdown  # noqa: E402


# --- Command parsing ------------------------------------------------------

@pytest.mark.parametrize("text", [
    "add GENI to my list",
    "Add GENI to my list.",
    "  add $GENI to my list  ",
])
def test_add_to_list_matches(text):
    assert tc.ADD_RE.match(text)


@pytest.mark.parametrize("text", [
    "add SPCX, APP to my watchlist",
    "Add SPCX and APP to my watchlist.",
    "add APP to my watchlist",
])
def test_add_to_watchlist_matches(text):
    assert tc.ADD_WATCHLIST_RE.match(text)


def test_watchlist_add_is_not_caught_by_list_add():
    """The bug that motivated these tests: "to my watchlist" must not be
    swallowed by the "to my list" pattern."""
    text = "add APP to my watchlist"
    assert tc.ADD_WATCHLIST_RE.match(text)
    assert not tc.ADD_RE.match(text)


def test_list_add_is_not_caught_by_watchlist_add():
    text = "add GENI to my list"
    assert tc.ADD_RE.match(text)
    assert not tc.ADD_WATCHLIST_RE.match(text)


# --- Markdown escaping ----------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Q3_2025 results", "Q3\\_2025 results"),
    ("Stock *soars*", "Stock \\*soars\\*"),
    ("[Update] on merger", "\\[Update] on merger"),
    ("plain headline", "plain headline"),
])
def test_escape_markdown(raw, expected):
    assert escape_markdown(raw) == expected


def test_escape_markdown_handles_empty_and_none():
    assert escape_markdown("") == ""
    assert escape_markdown(None) == ""


# --- News id hashing + migration -----------------------------------------

def test_article_key_is_short_and_stable():
    key = monitor._article_key("some-very-long-google-news-guid-" + "x" * 300)
    assert len(key) == 16
    assert key == monitor._article_key("some-very-long-google-news-guid-" + "x" * 300)


def test_article_key_differs_for_different_input():
    assert monitor._article_key("article-a") != monitor._article_key("article-b")


def test_migration_converts_raw_ids_once():
    raw = "https://news.example.com/an-article-guid"
    state = {"seen_news::GENI": [raw], "seen_news_google::GENI": [raw]}

    monitor._migrate_news_ids(state)

    expected = monitor._article_key(raw)
    assert state["seen_news::GENI"] == [expected]
    assert state["seen_news_google::GENI"] == [expected]
    assert state["news_id_scheme"] == monitor._NEWS_ID_SCHEME


def test_migration_is_idempotent():
    """Re-running must not hash the already-hashed values again, which
    would silently reset dedup and re-alert every stored article."""
    raw = "https://news.example.com/an-article-guid"
    state = {"seen_news::GENI": [raw]}

    monitor._migrate_news_ids(state)
    first = list(state["seen_news::GENI"])
    monitor._migrate_news_ids(state)

    assert state["seen_news::GENI"] == first


# --- Duplicate-headline detection ----------------------------------------

def test_near_identical_headlines_are_treated_as_duplicates():
    seen = ["genius sports announces q3 earnings beat"]
    assert monitor._is_duplicate_headline(
        "Genius Sports Announces Q3 Earnings Beat", seen
    )


def test_unrelated_headlines_are_not_duplicates():
    seen = ["genius sports announces q3 earnings beat"]
    assert not monitor._is_duplicate_headline(
        "Wolfspeed receives FDA clearance for new device", seen
    )


# --- Material news filter -------------------------------------------------

@pytest.mark.parametrize("headline", [
    "Analyst upgrades GENI to Buy",
    "Company announces acquisition of rival",
    "FDA approval granted for lead candidate",
])
def test_material_headlines_pass_filter(headline):
    assert monitor.is_material(headline)


@pytest.mark.parametrize("headline", [
    "5 stocks to watch this week",
    "What the market did today",
])
def test_immaterial_headlines_are_filtered_out(headline):
    assert not monitor.is_material(headline)


# --- IR feed results classifier -------------------------------------------
#
# This classifier decides whether to declare "earnings are out", so both
# directions matter: a missed release means no alert, a false positive means
# a partnership headline masquerading as results. The Cerebras case below is
# the one that caught a real bug -- the first version required a results word
# in the title and would have silently missed the very release the module
# was built for.

@pytest.mark.parametrize("title,body", [
    ("Cerebras Reports Second Quarter 2026 Financial Results", ""),
    ("Genius Sports Announces Q2 2026 Results", ""),
    ("XPeng Reports Fourth Quarter and Full Year 2025 Unaudited Financial Results", ""),
    # Title carries the period but no results word; body must carry it.
    ("Fast Inference Cloud Business Nearly Quadruples in Second Quarter 2026",
     "Revenue of $312 million, GAAP net loss of $41 million, gross margin of 58%."),
])
def test_real_results_releases_are_detected(title, body):
    assert ir_feeds.is_results_entry(title, body)


@pytest.mark.parametrize("title,body", [
    # Scheduling notices -- posted weeks ahead, carry every keyword.
    ("Cerebras Sets Date of Second Quarter 2026 Financial Results", ""),
    ("Wolfspeed to Report Fourth Quarter Fiscal 2026 Results on August 20", ""),
    ("Shift4 Announces Conference Call to Discuss Q2 2026 Results", ""),
    # Period reference, but not a results release and no financials in body.
    ("AppLovin Announces Strategic Partnership Expanding Q1 2026 Ad Reach",
     "The companies will collaborate on new advertising formats."),
    # No fiscal period marker at all.
    ("uniQure Announces FDA Clearance of IND Application", ""),
])
def test_non_results_entries_are_rejected(title, body):
    assert not ir_feeds.is_results_entry(title, body)


def test_empty_title_is_rejected():
    assert not ir_feeds.is_results_entry("", "revenue gaap per share")


# --- On-demand watch poll windows -----------------------------------------
#
# Polling only inside ON_DEMAND_POLL_WINDOWS_ET is what keeps a 24-hour watch
# from burning ~1,400 runs a day. Getting a boundary wrong silently means
# either wasted runs or a missed release, neither of which is visible until
# an earnings day.

def _at(hour, minute=0):
    return datetime(2026, 8, 12, hour, minute)


@pytest.mark.parametrize("moment", [
    _at(16, 0),    # after-close window opens exactly at the close
    _at(16, 5),    # when Cerebras actually published
    _at(17, 59),
    _at(18, 0),    # inclusive upper bound
    _at(6, 0),     # pre-market window opens
    _at(8, 45),
    _at(9, 0),     # inclusive upper bound
])
def test_moments_inside_poll_windows(moment):
    assert tc._in_poll_window(moment)


@pytest.mark.parametrize("moment", [
    _at(5, 59),    # one minute before the pre-market window
    _at(9, 1),     # one minute after it closes
    _at(12, 0),    # midday, between the two windows
    _at(15, 59),   # one minute before the close window
    _at(18, 1),    # one minute after it closes
    _at(23, 30),   # late evening
    _at(3, 0),     # overnight
])
def test_moments_outside_poll_windows(moment):
    assert not tc._in_poll_window(moment)


# --- Google News headline classifier --------------------------------------
#
# Detection falls back to news headlines for the 7 tickers with no IR feed,
# and newsrooms word things differently from company press releases. The
# false-positive direction is the dangerous one here: pre-earnings coverage
# mentions the same quarter and uses the same verbs, but is published days
# EARLY. A hit on one of those ends the watch, so nothing gets sent when the
# results actually land -- a silent failure, the worst kind.

@pytest.mark.parametrize("headline", [
    "Cerebras Reports Second Quarter 2026 Financial Results",
    "Cerebras Q2 revenue tops estimates",
    "XPeng posts Q4 2025 net loss as deliveries climb",
    "Genius Sports beats Q2 earnings estimates",
    "Wolfspeed misses on fourth-quarter revenue",
    "Shift4 Payments Q1 2026 earnings: EPS $1.02",
    "AppLovin reported second quarter 2026 results after the bell",
])
def test_results_coverage_is_detected(headline):
    assert ir_feeds.is_media_results_headline(headline)


@pytest.mark.parametrize("headline", [
    # Forward-looking: published before the release.
    "Analysts expect Cerebras to beat Q2 estimates",
    "Cerebras Q2 earnings preview: what to expect",
    "What to expect from XPeng's fourth quarter results",
    "Wolfspeed stock rises ahead of Q4 earnings",
    "Cerebras could beat Q2 revenue forecasts, analyst says",
    "Options traders brace for AppLovin Q2 earnings move",
    "Upcoming Q2 earnings: 5 stocks to watch",
    # Scheduling notices.
    "Genius Sports will report Q2 results on August 20",
    "Shift4 to announce first quarter 2026 results",
    "AppLovin sets date for second quarter 2026 earnings call",
    "Cerebras announces conference call to discuss Q2 2026 results",
    # Ordinary news with no fiscal period at all.
    "Cerebras announces partnership with major cloud provider",
    "XPeng launches new EV model in Europe",
])
def test_non_results_coverage_is_rejected(headline):
    assert not ir_feeds.is_media_results_headline(headline)


@pytest.mark.parametrize("raw,expected", [
    ("Cerebras Reports Q2 2026 Results - Reuters",
     "Cerebras Reports Q2 2026 Results"),
    ("XPeng posts Q4 loss - Business Wire", "XPeng posts Q4 loss"),
    ("No suffix here", "No suffix here"),
])
def test_google_news_outlet_suffix_is_stripped(raw, expected):
    assert ir_feeds._strip_source(raw) == expected


def test_hyphenated_headline_survives_suffix_stripping():
    # Naive splitting on "-" would mangle this; only a trailing outlet goes.
    assert "Full-year" in ir_feeds._strip_source(
        "Wolfspeed Full-year Results Beat - CNBC"
    )


# --- state.json three-way merge -------------------------------------------
#
# monitor.py and telegram_commands.py now run in separate concurrency groups,
# so they can push at the same time and one loses the race. The loser merges
# by key instead of rebasing -- a rebase can never succeed, since both sides
# rewrite every line of the same JSON file.
#
# The deletion case is the one that matters most: check_on_demand_earnings
# removes an ew_watch:: key once it has sent the release. If a merge revived
# it, the watch would resume and send the same earnings alert again.

def test_merge_keeps_both_sides_work():
    base = {"tg_update_offset": 100, "seen_news::CBRS": ["a"]}
    ours = {"tg_update_offset": 101, "seen_news::CBRS": ["a"]}
    theirs = {"tg_update_offset": 100, "seen_news::CBRS": ["a", "b"]}
    merged, _ = merge_state.merge(base, ours, theirs)
    assert merged["tg_update_offset"] == 101      # our command was handled
    assert merged["seen_news::CBRS"] == ["a", "b"]  # their dedup ids kept


def test_merge_is_symmetric():
    base = {"tg_update_offset": 100, "seen_news::CBRS": ["a"]}
    a = {"tg_update_offset": 101, "seen_news::CBRS": ["a"]}
    b = {"tg_update_offset": 100, "seen_news::CBRS": ["a", "b"]}
    assert merge_state.merge(base, a, b)[0] == merge_state.merge(base, b, a)[0]


def test_resolved_watch_stays_deleted():
    base = {"ew_watch::CBRS": {"armed": "t"}, "seen_news::GENI": ["p"]}
    ours = {"seen_news::GENI": ["p"]}
    theirs = {"ew_watch::CBRS": {"armed": "t"}, "seen_news::GENI": ["p", "q"]}
    merged, _ = merge_state.merge(base, ours, theirs)
    assert "ew_watch::CBRS" not in merged
    assert merged["seen_news::GENI"] == ["p", "q"]


def test_contested_deletion_defers_to_them():
    # They changed the value we were deleting, so they know something we
    # don't. Reviving the watch is the safer error than dropping it.
    base = {"ew_watch::CBRS": {"armed": "t"}}
    ours = {}
    theirs = {"ew_watch::CBRS": {"armed": "LATER"}}
    merged, _ = merge_state.merge(base, ours, theirs)
    assert merged["ew_watch::CBRS"] == {"armed": "LATER"}


def test_keys_we_did_not_touch_defer_to_them():
    merged, _ = merge_state.merge({"a": 1}, {"a": 1}, {"a": 9})
    assert merged["a"] == 9


def test_new_keys_from_both_sides_survive():
    merged, _ = merge_state.merge(
        {}, {"ew_watch::NVDA": {"armed": "t"}}, {"company_name::NVDA": "NVIDIA"}
    )
    assert merged == {"ew_watch::NVDA": {"armed": "t"},
                      "company_name::NVDA": "NVIDIA"}


def test_conflict_resolves_to_ours_and_is_counted():
    merged, report = merge_state.merge({}, {"k": 1}, {"k": 2})
    assert merged["k"] == 1
    assert report["conflicts"] == 1


@pytest.mark.parametrize("base,ours,theirs,expected", [
    ({}, {}, {}, {}),
    ({}, {"x": 1}, {}, {"x": 1}),   # nothing on the remote yet
    ({}, {}, {"y": 2}, {"y": 2}),   # this run changed nothing
])
def test_merge_handles_degenerate_inputs(base, ours, theirs, expected):
    assert merge_state.merge(base, ours, theirs)[0] == expected
