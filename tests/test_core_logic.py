"""Targeted tests for the logic that has actually broken before.

Deliberately narrow. These cover pure functions with no network calls, so
the suite runs in about a second and needs no secrets:

  * the Telegram command regexes (a watchlist/list collision was a real
    bug once -- "add X to my watchlist" must not be caught by the "add X
    to my list" pattern)
  * Markdown escaping of external text (unbalanced characters in a news
    headline used to make Telegram reject the whole message)
  * news-id hashing and the one-time migration of old raw ids
  * the three-stage news screen that replaced keyword matching
  * foreign-private-issuer classification and the early wire signal
  * the SEC fetch layer, including the gzip bug that meant earnings
    detection had never once worked

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

import gzip  # noqa: E402
import zlib  # noqa: E402

import early_signal  # noqa: E402
import earnings_watch  # noqa: E402
import llm_extract  # noqa: E402
import merge_state  # noqa: E402
import monitor  # noqa: E402
import news_filter  # noqa: E402
import sec_edgar  # noqa: E402
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


# --- News screening -------------------------------------------------------
#
# monitor.is_material() is gone. It was a substring match over ~40 keywords,
# and content farms write headlines containing exactly those words because
# that is how they rank. What replaced it is three stages in news_filter.py;
# these cover the two that need no model.

@pytest.mark.parametrize("source,title", [
    ("Zacks", "AMAT vs LRCX: which is the better value?"),
    ("The Motley Fool", "Why Wolfspeed stock popped today"),
    ("MarketBeat", "AppLovin trading down 4.7% on analyst downgrade"),
    # The publisher name arrives as a domain, not prose. Matching only
    # "simply wall st" let every one of this outlet's articles through.
    ("simplywall.st", "Shift4 could be 26% undervalued on cut guidance"),
    ("wallstreetzen", "uniQure upgraded to Hold"),
])
def test_blocked_publishers_are_dropped(source, title):
    assert not news_filter.source_allowed(source, title)


@pytest.mark.parametrize("title", [
    # Shapes that are never a company event, whoever published them. Each of
    # these is taken from an alert this bot actually sent.
    "5 AI stocks to watch this week",
    "Citigroup adjusts AppLovin price target to $600 from $650",
    "Truist Financial cuts Evolent Health price target to $6.00",
    "Erste Asset Management GmbH acquires new shares in AppLovin",
    "Shift4 Payments stock 12-month price target cut to $52.1",
    "Jim Cramer says buy NVDA",
    "XPEV price prediction for 2027",
])
def test_noise_shapes_are_dropped_from_any_source(title):
    assert not news_filter.source_allowed("Reuters", title)


@pytest.mark.parametrize("title", [
    "Applied Materials forecasts fourth-quarter revenue above estimates",
    "Genius Sports signs multi-year deal with the NFL",
    "Wolfspeed emerges from Chapter 11 bankruptcy",
    "XPeng recalls 47,000 vehicles over steering defect",
    "uniQure announces FDA breakthrough therapy designation for AMT-130",
    # A real ratings action must survive, even though price-target noise
    # does not. The distinction is the shape, not the vocabulary.
    "Morgan Stanley downgrades AppLovin to underweight",
])
def test_real_events_reach_the_classifier(title):
    assert news_filter.source_allowed("Reuters", title)


def test_alert_requires_subject_and_impact():
    """Both halves are needed.

    Impact alone passes "chip stocks surge on AI demand" -- genuinely
    high-impact news about somebody else. Subject alone passes every product
    blog post.
    """
    send, label = news_filter.should_alert(
        {"subject": True, "impact": "high", "event": "M&A", "why": "acquired X"}, "t")
    assert send and "M&A" in label

    assert not news_filter.should_alert(
        {"subject": False, "impact": "high", "event": "sector", "why": ""}, "t")[0]
    assert not news_filter.should_alert(
        {"subject": True, "impact": "medium", "event": "product", "why": ""}, "t")[0]


def test_no_model_falls_back_to_keywords_not_silence():
    assert news_filter.should_alert(None, "Applied Materials announces acquisition of X")[0]
    assert not news_filter.should_alert(None, "Applied Materials opens a new office")[0]


def test_prompts_survive_percent_signs():
    """Earnings releases and headlines are full of percentages.

    `PROMPT % (ticker, text)` raised "unsupported format character" on
    ordinary documents -- "Revenue increased 12%, driven by..." was enough.
    """
    for hazard in ("Revenue increased 12%, driven by deliveries",
                   "Margin of 100%s of plan", "Up 5%d", "Discount of 50%%",
                   "Cash at 90%(x)s", "Braces {like} {0} these"):
        assert hazard in llm_extract.build_prompt("XPEV", hazard)
        assert hazard in news_filter.build_prompt(f"0. [APP] {hazard}")


# --- Foreign private issuer classification --------------------------------

def test_foreign_issuer_is_read_from_the_forms_filed():
    """Form 6-K exists only for foreign private issuers, so this is
    definitional rather than a heuristic, and needs no maintained list."""
    assert sec_edgar.is_foreign_issuer([{"form": "6-K"}, {"form": "6-K"}])
    assert sec_edgar.is_foreign_issuer([{"form": "20-F"}])
    assert not sec_edgar.is_foreign_issuer([{"form": "8-K"}, {"form": "10-Q"}])
    assert not sec_edgar.is_foreign_issuer([])
    # A conversion long ago must not classify the company today.
    assert not sec_edgar.is_foreign_issuer([{"form": "8-K"}] * 40 + [{"form": "20-F"}])


# --- Early wire signal ----------------------------------------------------

@pytest.mark.parametrize("title", [
    "XPeng Inc. Announces Unaudited Second Quarter 2026 Financial Results",
    "Li Auto Inc. Reports Second Quarter 2026 Financial Results",
    "Genius Sports Reports Q2 2026 Results",
    "HSBC Holdings plc announces interim results for 2026",
])
def test_results_announcements_are_recognised(title):
    assert early_signal.looks_like_results(title)


@pytest.mark.parametrize("title", [
    # The scheduling notice is the dangerous one: it contains "report",
    # "second quarter" and "results", and publishes days early. The IR-page
    # scraper made exactly this mistake with Applied Materials.
    "XPeng to report second quarter results on August 24",
    "XPeng will announce Q2 2026 results next week",
    "XPeng to host second quarter 2026 earnings call",
    "XPeng Q2 2026 earnings preview: what to watch",
    "Analysts expect XPeng Q2 2026 results to show margin gains",
    "XPeng reports record June deliveries",
    "XPeng announces new P7 model launch",
])
def test_scheduling_and_preview_headlines_are_rejected(title):
    assert not early_signal.looks_like_results(title)


def test_only_wire_services_can_trigger_an_early_alert():
    for wire in ("Business Wire", "PR Newswire", "Reuters", "GlobeNewswire"):
        assert early_signal.source_is_wire(wire)
    for aggregator in ("Zacks", "The Motley Fool", "MarketBeat", "TipRanks"):
        assert not early_signal.source_is_wire(aggregator)


# --- SEC fetch layer ------------------------------------------------------

def test_gzip_responses_are_decompressed():
    """sec_edgar asks for gzip; urllib, unlike requests, does not decode it.

    Handing gzip bytes to json.loads failed every call into data.sec.gov,
    and did it invisibly: decoding with errors="replace" produces a string,
    so nothing raised until the JSON parser complained about column 1.
    """
    payload = b'{"ok": true}'
    assert sec_edgar._decompress(gzip.compress(payload), "gzip") == payload
    # A proxy that decompresses while leaving the header set must not
    # resurrect this, hence the magic-number check.
    assert sec_edgar._decompress(gzip.compress(payload), None) == payload
    assert sec_edgar._decompress(gzip.compress(payload), "identity") == payload
    assert sec_edgar._decompress(zlib.compress(payload), "deflate") == payload
    assert sec_edgar._decompress(payload, None) == payload


def test_acceptance_timestamps_are_eastern_not_utc():
    """EDGAR writes a trailing Z but the values are Eastern -- it only
    accepts filings 06:00-22:00 ET, so 06:45:59 cannot be UTC. Reading them
    as UTC puts every morning filing on the previous day."""
    parsed = earnings_watch._accepted_et({"accepted": "2026-08-24T06:45:59.000Z"})
    assert parsed is not None
    assert parsed.hour == 6 and parsed.strftime("%Y-%m-%d") == "2026-08-24"
    assert earnings_watch._accepted_et({"accepted": ""}) is None


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
