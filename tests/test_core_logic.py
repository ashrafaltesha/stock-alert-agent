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
