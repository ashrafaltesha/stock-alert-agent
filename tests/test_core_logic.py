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

import merge_state  # noqa: E402
import sec_edgar  # noqa: E402
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


# --- SEC filing earnings detection ----------------------------------------
#
# Replaces the IR-page scraping that came before it. That approach worked for
# roughly half the holdings; sites like Genius Sports and AppLovin serve
# nothing to an automated reader at all. Filing with the SEC is a legal
# obligation, so coverage is universal.

@pytest.mark.parametrize("items", ["2.02", "2.02,9.01", "9.01, 2.02", "1.01,2.02,9.01"])
def test_item_202_is_earnings(items):
    assert sec_edgar.is_domestic_earnings({"form": "8-K", "items": items})


@pytest.mark.parametrize("items", ["5.02", "8.01,9.01", "", "1.01"])
def test_other_8k_items_are_not_earnings(items):
    assert not sec_edgar.is_domestic_earnings({"form": "8-K", "items": items})


@pytest.mark.parametrize("items", ["12.02", "2.021"])
def test_item_codes_are_not_substring_matched(items):
    # A plain `in` test makes "2.02" match "12.02". No such item exists today,
    # but a code added later would silently start firing false earnings
    # alerts -- and a false alert is expensive to trust once and then doubt.
    assert not sec_edgar.is_domestic_earnings({"form": "8-K", "items": items})


def test_6k_never_matches_the_domestic_rule():
    assert not sec_edgar.is_domestic_earnings({"form": "6-K", "items": "2.02"})


def test_missing_keys_do_not_raise():
    assert not sec_edgar.is_domestic_earnings({"form": "4"})
    assert not sec_edgar.is_domestic_earnings({"form": "8-K", "items": None})


@pytest.mark.parametrize("score", [5, 6, 9, 14])
def test_foreign_scores_at_or_above_threshold_are_earnings(score):
    # 5 is the floor because Honda's quarterlies score 5-6 EVERY quarter
    # (Aug 5, May 14, Feb 10, Nov 7). A threshold of 7 -- which a smaller
    # 20-issuer sample suggested -- would have missed Honda four times a year.
    assert sec_edgar.is_foreign_earnings(score, True)


@pytest.mark.parametrize("score", [0, 1, 2, 4])
def test_foreign_scores_below_threshold_are_rejected(score):
    assert not sec_edgar.is_foreign_earnings(score, True)


def test_period_reference_is_required():
    # JD filed a 23KB 6-K mentioning a quarter but containing no financial
    # terms; Alibaba filed ones with a term but no period. Both halves needed.
    assert not sec_edgar.is_foreign_earnings(13, False)
    assert not sec_edgar.is_foreign_earnings(0, True)


def test_xbrl_render_fragments_are_excluded():
    # Rio Tinto's filings surface R12.htm etc -- XBRL viewer renderings full
    # of financial vocabulary that would inflate the score of any filing.
    assert sec_edgar._XBRL_RENDER_RE.match("R12.htm")
    assert not sec_edgar._XBRL_RENDER_RE.match("d143720dex991.htm")
    assert not sec_edgar._XBRL_RENDER_RE.match("Report.htm")


def test_filing_url_strips_leading_zeros_from_cik():
    # EDGAR's Archives paths use the unpadded CIK; the submissions API uses
    # the padded one. Mixing them up yields a 404.
    assert sec_edgar.filing_url("0000006951", "0000006951-26-000045", "a8-k.htm") == (
        "https://www.sec.gov/Archives/edgar/data/6951/000000695126000045/a8-k.htm"
    )


def test_script_contents_are_stripped_before_scoring():
    # Otherwise a page's JavaScript could supply the financial vocabulary.
    assert sec_edgar.strip_html("<script>var revenue='x'</script><p>Hi</p>") == "Hi"


def test_fetch_failure_is_distinguishable_from_a_low_score():
    # Vale scored 1 on one run and 12 on the next for the same filing: a
    # swallowed network error read as "not earnings". A missed earnings
    # report must never look like normal operation.
    assert issubclass(sec_edgar.FetchError, Exception)
