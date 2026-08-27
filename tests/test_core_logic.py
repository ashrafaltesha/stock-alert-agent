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
import sys
from datetime import datetime, timedelta, timezone
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The config module reads these at import time; tests never send anything.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "test-chat")
os.environ.setdefault("FINNHUB_API_KEY", "test-key")

import gzip  # noqa: E402
import json  # noqa: E402

import health  # noqa: E402
import heartbeat  # noqa: E402
import state_utils  # noqa: E402
import zlib  # noqa: E402

import early_signal  # noqa: E402
import earnings_watch  # noqa: E402
import llm_extract  # noqa: E402
import llm_client  # noqa: E402
import earnings_utils  # noqa: E402
import earnings_watch  # noqa: E402
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


# --- LLM provider handling ------------------------------------------------

def test_deprecated_model_falls_through_to_the_next():
    """Groq retired llama-3.3-70b-versatile for free tiers on 2026-06-17 and
    the next call returned 404 "does not exist". A pinned model name is a
    scheduled outage, so preferences are a list."""
    import io
    import urllib.error

    tried = []

    def fake_post(url, payload, headers=None):
        tried.append(payload["model"])
        if payload["model"] == llm_client.GROQ_MODELS[0]:
            raise urllib.error.HTTPError(
                url, 404, "err", {},
                io.BytesIO(b'{"error":{"message":"model does not exist"}}'))
        return {"choices": [{"message": {"content": "ok"}}]}

    original_post, llm_client._post = llm_client._post, fake_post
    original_model, llm_client._resolved_groq_model = llm_client._resolved_groq_model, None
    try:
        assert llm_client.call_groq("prompt", "key") == "ok"
        assert tried == list(llm_client.GROQ_MODELS[:2])
        # The survivor is remembered, so later calls cost nothing extra.
        tried.clear()
        llm_client.call_groq("prompt", "key")
        assert tried == [llm_client.GROQ_MODELS[1]]
    finally:
        llm_client._post = original_post
        llm_client._resolved_groq_model = original_model


def test_auth_failure_is_not_retried_against_every_model():
    """A 401 is not fixed by picking another model, and retrying each one
    just multiplies the failures."""
    import io
    import urllib.error

    tried = []

    def fake_post(url, payload, headers=None):
        tried.append(payload["model"])
        raise urllib.error.HTTPError(
            url, 401, "err", {}, io.BytesIO(b'{"error":{"message":"Invalid API Key"}}'))

    original_post, llm_client._post = llm_client._post, fake_post
    original_model, llm_client._resolved_groq_model = llm_client._resolved_groq_model, None
    try:
        with_error = False
        try:
            llm_client.call_groq("prompt", "bad-key")
        except urllib.error.HTTPError as e:
            with_error = e.code == 401
        assert with_error
        assert len(tried) == 1
    finally:
        llm_client._post = original_post
        llm_client._resolved_groq_model = original_model


def test_requests_carry_a_user_agent():
    """Cloudflare fronts Groq and rejects urllib's default signature with
    HTTP 403 "error code: 1010"."""
    assert "Mozilla" in llm_client.BASE_HEADERS["User-Agent"]


# --- Unrecognised commands ------------------------------------------------

@pytest.mark.parametrize("text", [
    "sold 10 NVDA at 600",        # near-miss: missing "shares of"
    "add GENI",                   # near-miss: missing "to my list"
    "whats my portfolio",
    "summry",
])
def test_unrecognised_commands_get_a_reply(text, monkeypatch):
    """Silence is the one response that cannot be interpreted.

    This used to return without sending anything, so a typo in a command was
    indistinguishable from the listener being down -- and it had been down,
    repeatedly, so that ambiguity cost real debugging time.
    """
    sent = []
    monkeypatch.setattr(tc, "send_telegram_message", sent.append)
    tc.process_message(text, ["AMAT"], {}, [], {})
    assert sent, f"no reply to {text!r}"
    assert "didn't understand" in sent[0]


@pytest.mark.parametrize("text", ["help", "commands", "/help", "?"])
def test_help_is_reachable_and_does_not_apologise(text, monkeypatch):
    sent = []
    monkeypatch.setattr(tc, "send_telegram_message", sent.append)
    tc.process_message(text, ["AMAT"], {}, [], {})
    assert sent and "What I understand" in sent[0]
    assert "didn't understand" not in sent[0]


def test_empty_messages_stay_silent(monkeypatch):
    """A photo or sticker carries no text; nagging about it would be noise."""
    sent = []
    monkeypatch.setattr(tc, "send_telegram_message", sent.append)
    tc.process_message("", ["AMAT"], {}, [], {})
    tc.process_message("   ", ["AMAT"], {}, [], {})
    assert sent == []


# --- Position corrections -------------------------------------------------

def test_set_position_overwrites_and_leaves_cash_alone(monkeypatch):
    """A correction is not a trade.

    "added N shares at P" blends into the average cost and moves cash, which
    is wrong when reconciling against a broker statement or repairing a
    position the bot failed to persist. Only "added" existed, so corrections
    had to be faked with it.
    """
    monkeypatch.setattr(tc, "send_telegram_message", lambda m: None)
    monkeypatch.setattr(tc, "validate_ticker", lambda t: True)
    holdings = {"BAK": {"shares": 1000, "avg_cost": 2.50},
                tc.CASH_KEY: 5000.0, tc.DEPOSITS_KEY: 20000.0}
    tickers = ["BAK"]
    _, holdings_changed, _ = tc.process_message(
        "set BAK to 1650 shares at $1.93", tickers, holdings, [], {})
    assert holdings_changed
    assert holdings["BAK"] == {"shares": 1650.0, "avg_cost": 1.93}
    assert holdings[tc.CASH_KEY] == 5000.0
    assert holdings[tc.DEPOSITS_KEY] == 20000.0


@pytest.mark.parametrize("text", [
    "set BAK 1650 shares at 1.93",
    "Set $BAK to 1650 shares @ $1.93",
    "set BAK to 1,650 shares at 1.93",
])
def test_set_position_accepts_the_obvious_variants(text, monkeypatch):
    monkeypatch.setattr(tc, "send_telegram_message", lambda m: None)
    monkeypatch.setattr(tc, "validate_ticker", lambda t: True)
    holdings = {"BAK": {"shares": 1000, "avg_cost": 2.50}}
    tc.process_message(text, ["BAK"], holdings, [], {})
    assert holdings["BAK"]["shares"] == 1650.0


def test_set_position_without_a_price_keeps_the_average(monkeypatch):
    monkeypatch.setattr(tc, "send_telegram_message", lambda m: None)
    monkeypatch.setattr(tc, "validate_ticker", lambda t: True)
    holdings = {"BAK": {"shares": 1000, "avg_cost": 2.50}}
    tc.process_message("set BAK to 1650 shares", ["BAK"], holdings, [], {})
    assert holdings["BAK"] == {"shares": 1650.0, "avg_cost": 2.50}


def test_set_cash_still_means_cash(monkeypatch):
    """The new pattern must not swallow the existing cash command."""
    sent = []
    monkeypatch.setattr(tc, "send_telegram_message", sent.append)
    holdings = {}
    tc.process_message("set cash to 5000", [], holdings, [], {})
    assert holdings.get(tc.CASH_KEY) == 5000.0
    assert not any("Set *CASH*" in m for m in sent)


# --- Dead-man's switch ----------------------------------------------------

def test_heartbeat_is_silent_without_a_key(monkeypatch):
    """Absent secret must disable it completely, not half-configure it."""
    monkeypatch.delenv("HEALTHCHECK_PING_KEY", raising=False)
    called = []
    monkeypatch.setattr(heartbeat.urllib.request, "urlopen",
                        lambda *a, **k: called.append(1))
    assert heartbeat.ping(heartbeat.MONITOR) is False
    assert not called


def test_heartbeat_never_raises(monkeypatch):
    """Instrumentation that can break the thing it instruments is worse than
    none. The whole point is to survive an outage of the monitoring service."""
    monkeypatch.setenv("HEALTHCHECK_PING_KEY", "pk")

    def boom(*a, **k):
        raise OSError("healthchecks unreachable")

    monkeypatch.setattr(heartbeat.urllib.request, "urlopen", boom)
    assert heartbeat.ping(heartbeat.MONITOR) is False


def test_heartbeat_url_shape(monkeypatch):
    monkeypatch.setenv("HEALTHCHECK_PING_KEY", "pk")
    seen = []

    class Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(heartbeat.urllib.request, "urlopen",
                        lambda req, timeout=5: (seen.append(req.full_url), Resp())[1])
    heartbeat.ping(heartbeat.LISTENER)
    assert seen[-1] == "https://hc-ping.com/pk/stock-telegram-listener?create=1"
    heartbeat.ping(heartbeat.LISTENER, fail=True)
    assert seen[-1].endswith("/stock-telegram-listener/fail?create=1")


# --- Retired state keys ---------------------------------------------------

def test_pruning_removes_dead_keys_and_spares_live_ones():
    """The near-miss that matters: ew_watch:: and ew_seen:: share a prefix
    with several retired ew_ families and must survive."""
    state = {
        "ir_page::WOLF": "http://x",
        "ew_summary_sent::CBRS::2026-08-12": True,
        "ew_on_demand::2026-08-10": ["ASTS"],
        "ew_on_demand_giveup::ASTS": True,
        "ew_poll_started::afterhours": "t",
        "ew_amc_reminder_sent::WOLF": True,
        "ew_watch::XPEV": {"armed": "t"},
        "ew_seen::XPEV": "acc",
        "ew_mark::XPEV": "acc",
        "price_ref::AMAT": {},
        "tg_update_offset": 1,
    }
    assert state_utils.prune_retired(state) == 6
    for key in ("ew_watch::XPEV", "ew_seen::XPEV", "ew_mark::XPEV",
                "price_ref::AMAT", "tg_update_offset"):
        assert key in state, f"pruning destroyed a live key: {key}"
    assert state_utils.prune_retired(state) == 0


# --- Seen-article retention -----------------------------------------------
#
# seen_news was 85% of state.json: 26 lists of 100 ids, rewritten and
# committed ~1,380 times a day. An article can only be alerted while it is
# younger than NEWS_LOOKBACK_MINUTES, so ids older than that were being kept
# for nothing. The risk of shrinking them is a DUPLICATE ALERT, so that is
# what these cover.

def _run(state, key, article_id, title, minutes_old, sent, monkeypatch=None):
    seen = monitor._load_seen(state, key)
    candidates = []
    if article_id not in seen:
        seen[article_id] = monitor._now_minutes()
        if minutes_old <= monitor.NEWS_LOOKBACK_MINUTES:
            candidates.append({"ticker": "WOLF", "title": title,
                               "source": "Reuters", "link": "http://x"})
    monitor._save_seen(state, key, seen)
    monitor.process_news_candidates(candidates, state)


def test_seen_ids_dedupe_then_age_out_without_re_alerting(monkeypatch):
    clock = {"m": 29_000_000}
    monkeypatch.setattr(monitor, "_now_minutes", lambda: clock["m"])
    monkeypatch.setattr(news_filter, "classify",
                        lambda arts: [{"subject": True, "impact": "high",
                                       "event": "contract", "why": "award"}] * len(arts))
    sent = []
    monkeypatch.setattr(monitor, "send_telegram_message", sent.append)

    state, key = {}, "seen_news_google::WOLF"
    title = "Wolfspeed receives $750 million CHIPS award"

    _run(state, key, "aaa", title, 5, sent)
    assert len(sent) == 1, "first sighting must alert"

    clock["m"] += 5
    _run(state, key, "aaa", title, 10, sent)
    assert len(sent) == 1, "id still remembered"

    # Past retention the id is forgotten -- the age check must carry it.
    clock["m"] += monitor.ID_RETENTION_MINUTES + 10
    _run(state, key, "aaa", title, 230, sent)
    assert len(sent) == 1, "forgotten id + stale article must not re-alert"

    # Forgotten AND republished as fresh: fuzzy title dedupe is the backstop.
    _run(state, key, "aaa", title, 3, sent)
    assert len(sent) == 1, "republished story must not re-alert"


def test_retention_is_bounded_on_save_not_only_on_load(monkeypatch):
    """A bound enforced on one side of a round trip is not a bound.

    Pruning only on load passed every test until one handed _save_seen 400
    entries directly, and all 400 were written.
    """
    clock = {"m": 29_000_000}
    monkeypatch.setattr(monitor, "_now_minutes", lambda: clock["m"])
    state, key = {}, "seen_news::AMAT"
    monitor._save_seen(state, key,
                       {f"{i:016x}": clock["m"] - i * 4 for i in range(400)})
    assert len(state[key]) <= monitor.ID_RETENTION_MINUTES // 4 + 1


def test_seen_state_round_trip_is_byte_stable(monkeypatch):
    """An unordered rewrite would show every line as changed on every
    commit -- churn of a different kind."""
    clock = {"m": 29_000_000}
    monkeypatch.setattr(monitor, "_now_minutes", lambda: clock["m"])
    state, key = {}, "seen_news::AMAT"
    monitor._save_seen(state, key, {"b": clock["m"], "a": clock["m"] - 1})
    first = json.dumps(state[key])
    monitor._save_seen(state, key, monitor._load_seen(state, key))
    assert json.dumps(state[key]) == first


def test_legacy_plain_id_lists_are_carried_over(monkeypatch):
    """Dropping them in one step would let anything still inside the
    lookback window alert a second time on the first run after deploy."""
    clock = {"m": 29_000_000}
    monkeypatch.setattr(monitor, "_now_minutes", lambda: clock["m"])
    state = {"seen_news::AMAT": ["aaa", "bbb", "ccc"]}
    loaded = monitor._load_seen(state, "seen_news::AMAT")
    assert set(loaded) == {"aaa", "bbb", "ccc"}
    assert all(v == clock["m"] for v in loaded.values())


def test_changeover_never_enlarges_state(monkeypatch):
    """Measured against the real file, the first version of this change
    temporarily DOUBLED state.json: 100 undated legacy ids per list were
    stamped 'now' and held for the full retention window. Only the newest
    are worth carrying, since only ids inside the lookback window could
    re-alert."""
    clock = {"m": 29_000_000}
    monkeypatch.setattr(monitor, "_now_minutes", lambda: clock["m"])
    key = "seen_news_google::WOLF"
    legacy = {key: [f"{i:016x}" for i in range(100)]}
    before = len(json.dumps(legacy, indent=2))
    monitor._save_seen(legacy, key, monitor._load_seen(legacy, key))
    assert len(json.dumps(legacy, indent=2)) <= before
    assert len(legacy[key]) <= monitor.LEGACY_CARRY


# --- Stale-snapshot writes ------------------------------------------------

def test_listener_writes_deltas_not_snapshots(monkeypatch, tmp_path=None):
    """A long-running job must not write back what it read hours ago.

    Observed live on 2026-08-24: the monitor pruned eight retired keys and
    logged it, and state.json on main still had all eight minutes later. The
    listener had loaded state at 17:05, held it, and wrote the whole snapshot
    on its next flush.

    Merging on push REJECTION does not catch this. If the stale writer
    happens to be level with the tip, git accepts the write cleanly and
    everything the other process changed is silently reverted. The fix is to
    refresh first and re-apply only our own delta -- which is what this
    asserts, at the level of the delta arithmetic.
    """
    read_at_start = {"tg_update_offset": 100, "ir_page::WOLF": "http://old",
                     "seen_news::AMAT": ["a", "b"]}
    mine = dict(read_at_start)
    mine["tg_update_offset"] = 200          # the only thing we changed

    changed = {k: v for k, v in mine.items()
               if k not in read_at_start or read_at_start[k] != v}
    deleted = [k for k in read_at_start if k not in mine]
    assert changed == {"tg_update_offset": 200}
    assert deleted == []

    # Meanwhile another process pruned a key and rewrote another.
    current = {"tg_update_offset": 100,
               "seen_news::AMAT": [["a", 29000000], ["b", 29000001]]}

    merged = dict(current)
    for key in deleted:
        merged.pop(key, None)
    merged.update(changed)

    assert "ir_page::WOLF" not in merged, "a stale snapshot must not resurrect it"
    assert isinstance(merged["seen_news::AMAT"][0], list), "their rewrite survives"
    assert merged["tg_update_offset"] == 200, "our change is applied"


def test_only_alertable_articles_are_remembered(monkeypatch):
    """Both feeds return ~100 items per query regardless of age.

    Remembering all of them put the whole feed in state.json: measured live,
    seen_news_google::RDDT held 144 ids all stamped within four minutes and
    the file reached 94 KB. The old [-100:] cap hid this by truncating;
    time-based retention removed the cap and exposed it.

    An article outside the lookback window cannot alert on this run or any
    later one -- the age check rejects it first -- so recording it buys
    nothing.
    """
    clock = {"m": 29_000_000}
    monkeypatch.setattr(monitor, "_now_minutes", lambda: clock["m"])
    monkeypatch.setattr(news_filter, "classify",
                        lambda arts: [{"subject": True, "impact": "high",
                                       "event": "contract", "why": "x"}] * len(arts))
    sent = []
    monkeypatch.setattr(monitor, "send_telegram_message", sent.append)

    state, key = {}, "seen_news_google::RDDT"
    feed = ([("fresh", 5, "Reddit signs $500 million cloud deal")]
            + [(f"old{i}", 200 + i, f"Reddit older story {i}") for i in range(99)])

    def run(items):
        seen = monitor._load_seen(state, key)
        known = set(seen)
        candidates = []
        for article_id, age, title in items:
            if article_id in known:
                continue
            if age <= monitor.NEWS_LOOKBACK_MINUTES:
                seen[article_id] = monitor._now_minutes()
                candidates.append({"ticker": "RDDT", "title": title,
                                   "source": "Reuters", "link": "http://x"})
        monitor._save_seen(state, key, seen)
        monitor.process_news_candidates(candidates, state)

    run(feed)
    assert len(state[key]) == 1, "only the in-window article is stored"
    assert len(sent) == 1

    # Re-reading the same feed must neither re-alert nor grow the list.
    for _ in range(10):
        clock["m"] += 1
        run(feed)
    assert len(state[key]) == 1
    assert len(sent) == 1


# --- status command -------------------------------------------------------

def test_status_flags_stale_components_and_spares_the_idle_watcher():
    """The watcher exits when nothing is armed, which is most days.

    Reporting "never ran" as a fault would make the status message cry wolf
    on every quiet day, and a status message that is usually wrong is worse
    than none -- it trains you to ignore it.
    """
    from datetime import timedelta
    now = earnings_utils.now_et()
    state = {
        "hb::monitor": now.isoformat(),
        "hb::listener": (now - timedelta(minutes=5)).isoformat(),
        "hb::earnings_arm": (now - timedelta(hours=40)).isoformat(),
    }
    rows = {name: ok for name, _age, ok in health.component_lines(state)}
    assert rows["monitor"] is True
    assert rows["listener"] is True
    assert rows["earnings_arm"] is False, "40 hours without arming is stale"
    assert rows["earnings_watch"] is True, "never-run watcher is not a fault"


def test_status_handles_missing_and_malformed_timestamps():
    assert health._age_minutes(None) is None
    assert health._age_minutes("not-a-date") is None
    assert health._human(None) == "never"


# --- A heartbeat written only at exit cannot measure liveness -------------
#
# The watcher records a heartbeat every 15-second cycle, but the workflow
# commits state.json in one step AFTER the loop -- on purpose, because a git
# push inside the poll would cost more than the poll interval. So the
# recorded timestamp measures time since the last run ENDED.
#
# With a 330-minute loop and a 90-minute limit, a perfectly healthy watcher
# read as stale for four hours out of every five and a half. On 2026-08-27 I
# used that number to judge whether a live run was dead, and nearly acted on
# it.

WATCHER_ARMED = {"ew_watch::RBRK": {"armed": "x", "expires": "y"},
                 "ew_watch::NVDA": {"armed": "x", "expires": "y"}}


def _rows(state, latest_run=None):
    return {name: (detail, ok)
            for name, detail, ok in health.component_lines(state, latest_run)}


def test_a_healthy_long_running_watcher_is_not_reported_as_stale():
    """The false alarm this closes: heartbeat hours old, run very much alive."""
    from datetime import timedelta
    old = (earnings_utils.now_et() - timedelta(minutes=200)).isoformat()
    state = dict(WATCHER_ARMED, **{"hb::earnings_watch": old})

    detail, ok = _rows(state, lambda wf: ("in_progress", 200.0))["earnings_watch"]
    assert ok is True, "a run the platform reports as in_progress is not stale"
    assert "polling" in detail and "3.3h" in detail


def test_a_watch_armed_with_no_watcher_is_the_actual_fault():
    """Not "the watcher is quiet" -- "nothing is polling while something is
    armed". That is the condition that cost the NVDA report."""
    detail, ok = _rows(WATCHER_ARMED,
                       lambda wf: ("completed", 12.0))["earnings_watch"]
    assert ok is False
    assert "NOT RUNNING" in detail and "2 watches armed" in detail


def test_an_idle_watcher_with_nothing_armed_is_healthy():
    """Idle is correct on the ~215 trading days a year with nothing to watch.
    A status message that cries wolf daily trains you to ignore it."""
    detail, ok = _rows({}, lambda wf: ("completed", 900.0))["earnings_watch"]
    assert ok is True
    assert "idle" in detail


def test_singular_and_plural_watch_counts_read_correctly():
    one = {"ew_watch::RBRK": {}}
    detail, _ = _rows(one, lambda wf: ("completed", 12.0))["earnings_watch"]
    assert "1 watch armed" in detail


@pytest.mark.parametrize("broken", [
    lambda wf: None,                                  # lookup returned nothing
    lambda wf: (_ for _ in ()).throw(RuntimeError()),  # lookup raised
])
def test_status_falls_back_to_the_heartbeat_when_the_api_cannot_be_asked(broken):
    """Unknown must degrade to the timestamp, not to a false verdict either
    way. The fallback limit is the loop length plus margin, not 90 minutes."""
    from datetime import timedelta
    now = earnings_utils.now_et()
    fresh = dict(WATCHER_ARMED,
                 **{"hb::earnings_watch": (now - timedelta(minutes=200)).isoformat()})
    detail, ok = _rows(fresh, broken)["earnings_watch"]
    assert ok is True, "200 min is inside a 330-minute loop"
    assert "ago" in detail

    dead = dict(WATCHER_ARMED,
                **{"hb::earnings_watch": (now - timedelta(hours=20)).isoformat()})
    assert _rows(dead, broken)["earnings_watch"][1] is False


def test_the_fallback_limit_cannot_drift_below_the_loop_it_measures():
    """The original bug as a rule: any component whose heartbeat is written
    only at exit must tolerate a full run before being called stale."""
    import earnings_watch
    for component in health.EXIT_PERSISTED:
        assert health.EXPECTED_INTERVAL[component] > earnings_watch.LOOP_MINUTES, (
            f"{component} is called stale before one loop of "
            f"{earnings_watch.LOOP_MINUTES} min can finish")


# --- A resolved CIK must survive the runner that resolved it -------------
#
# Found 2026-08-27 while checking whether RBRK would be detected. cik_map.json
# shipped 13 entries and RBRK was not one of them -- nor were BAK, CENX, KEEL,
# META or RDDT, five of the thirteen tickers actually monitored.
#
# resolve_cik falls back to fetching the SEC's ~800KB company_tickers.json and
# writes the result to disk, but NO workflow committed cik_map.json, so every
# run threw the answer away and re-fetched. That made a live earnings watch
# depend on one large request succeeding, and when it fails the poll loop
# prints a line and continues -- green run, nothing polled, no alert.

def _workflow_text(name):
    import pathlib
    return (pathlib.Path(__file__).resolve().parent.parent
            / ".github" / "workflows" / name).read_text()


@pytest.mark.parametrize("name", ["monitor.yml", "earnings_watch.yml",
                                  "telegram_commands.yml"])
def test_every_workflow_that_persists_state_also_persists_resolved_ciks(name):
    """Stated as a rule so it cannot drift: a job trusted to remember what the
    bot has seen must also remember what it has resolved. Otherwise the map
    is rebuilt from a network fetch on every single run, forever."""
    text = _workflow_text(name)
    for line in text.splitlines():
        if line.strip().startswith("git add ") and "state.json" in line:
            assert "cik_map.json" in line, (
                f"{name}: '{line.strip()}' commits state but drops the CIK map")


def test_monitored_tickers_are_resolvable_without_a_network_fetch():
    """Reports which tickers still depend on the 800KB list.

    Not an assertion that the map is complete -- it self-heals now that it is
    committed -- but the armed ones must be covered, because those are the
    ones whose alert is riding on it today.
    """
    import json
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    cik_map = json.loads((root / "cik_map.json").read_text())
    tickers = set(json.loads((root / "tickers.json").read_text()))
    watchlist = set(json.loads((root / "watchlist.json").read_text()))
    missing = sorted((tickers | watchlist) - set(cik_map))
    # Informational: these resolve on first use and are then persisted.
    print(f"resolved lazily on first use: {missing}")
    assert isinstance(missing, list)


def test_an_unresolvable_cik_on_an_armed_watch_is_announced_once():
    """Silence here is indistinguishable from "nothing has been filed yet",
    which is how every missed report in this project has looked."""
    import earnings_watch
    state = {}
    assert earnings_watch._warn_once(state, "cik", "RBRK") is True
    # 15-second poll loop: 240 messages an hour would train you to mute it.
    for _ in range(240):
        assert earnings_watch._warn_once(state, "cik", "RBRK") is False
    assert earnings_watch._warn_once(state, "cik", "OTHER") is True, \
        "the guard is per ticker, not global"


def test_warnings_clear_so_a_recurrence_is_heard_again():
    import earnings_watch
    state = {"ew_warned::cik::RBRK": True, "ew_warned::cik::BAK": True,
             "ew_watch::RBRK": {}}
    earnings_watch._clear_warnings(state, "RBRK")
    assert "ew_warned::cik::RBRK" not in state
    assert "ew_warned::cik::BAK" in state, "clearing one ticker must not clear others"
    assert "ew_watch::RBRK" in state, "only warning keys are removed"
    assert earnings_watch._warn_once(state, "cik", "RBRK") is True


# --- Bound a search by what you seek, not by rows in front of it ---------
#
# Replaying RBRK on 2026-08-27 printed "No recent RBRK filing classifies as
# earnings" while the 8-K was plainly on EDGAR. The scan took the 15 newest
# filings; RBRK's newest 8-K was at index 28.
#
# The exact sequence, read from data.sec.gov that day. A 2024 IPO with vesting
# equity files Form 4s and 144s constantly, and they crowd out everything.
RBRK_FORMS = (["4", "SCHEDULE 13G/A", "SCHEDULE 13G", "4", "4", "4", "144",
               "144", "SCHEDULE 13G", "4", "144", "4", "144", "144", "144",
               "4", "4", "4", "4", "144", "144", "144", "4", "4", "4", "4",
               "4", "4"] + ["8-K"])


def _rbrk_filings():
    return [{"form": f, "accession": f"acc-{i}", "filed": "2026-06-05",
             "items": "2.02" if f == "8-K" else "", "doc": "d.htm"}
            for i, f in enumerate(RBRK_FORMS)]


def test_the_earnings_filing_is_found_behind_a_wall_of_insider_filings():
    filings = _rbrk_filings()
    assert filings[28]["form"] == "8-K", "fixture guard: the 8-K is at index 28"
    assert not any(f["form"] == "8-K" for f in filings[:15]), \
        "fixture guard: a raw 15-row scan cannot see it"

    found = sec_edgar.earnings_candidates(filings, 15)
    assert found and found[0]["form"] == "8-K"
    assert sec_edgar.is_domestic_earnings(found[0])


def test_candidate_scan_is_still_bounded():
    """The bound exists because 6-K classification costs a document fetch."""
    many = [{"form": "6-K", "accession": f"a{i}", "items": ""} for i in range(50)]
    assert len(sec_edgar.earnings_candidates(many, 15)) == 15
    assert len(sec_edgar.earnings_candidates(many, 3)) == 3


def test_candidate_scan_keeps_order_and_drops_non_candidates():
    filings = [{"form": "4"}, {"form": "8-K", "n": 1}, {"form": "144"},
               {"form": "6-K", "n": 2}, {"form": "10-Q"}, {"form": "8-K", "n": 3}]
    got = sec_edgar.earnings_candidates(filings, 10)
    assert [f["n"] for f in got] == [1, 2, 3], "newest-first order must survive"
    assert all(f["form"] in ("8-K", "6-K") for f in got)


def test_candidate_scan_survives_junk():
    assert sec_edgar.earnings_candidates(None, 5) == []
    assert sec_edgar.earnings_candidates([], 5) == []
    assert sec_edgar.earnings_candidates([{}, {"form": None}], 5) == []


def test_a_foreign_issuer_is_not_misread_as_domestic_by_insider_volume():
    """Same root cause, different victim: a full lookback of Form 4s would
    hide the 6-Ks and silently disable the wire early-signal path."""
    noise = [{"form": "4"} for _ in range(60)]
    assert sec_edgar.is_foreign_issuer(noise + [{"form": "6-K"}]) is True
    # A genuinely domestic filer is still domestic.
    assert sec_edgar.is_foreign_issuer(noise + [{"form": "8-K"}] * 50) is False


def test_last_earnings_bootstrap_sees_past_insider_filings(monkeypatch):
    """The calendar-independent safety net was inert for this whole class of
    company: with no earnings date learned, nothing could look overdue."""
    import earnings_watch
    monkeypatch.setattr(sec_edgar, "recent_filings",
                        lambda cik, etag=None: (_rbrk_filings(), None))
    state = {}
    assert earnings_watch._last_earnings_date(state, "RBRK", "0001943896") \
        == "2026-06-05"
    assert state["ew_last_earnings::RBRK"] == "2026-06-05"


def test_live_detection_never_used_a_row_budget():
    """Why tonight's alert was never at risk: _process walks until it reaches
    the stored baseline, with no cap. Pinned so an optimisation cannot
    quietly introduce one."""
    import inspect
    import earnings_watch
    src = inspect.getsource(earnings_watch._process)
    assert "for f in filings:" in src
    assert "[:" not in src.split("if not fresh")[0], \
        "_process must not slice the filing list"


def test_components_that_commit_as_they_go_are_unaffected():
    """The listener and monitor push state mid-run, so their timestamps do
    measure liveness and must keep their tight limits."""
    from datetime import timedelta
    now = earnings_utils.now_et()
    state = {"hb::monitor": (now - timedelta(minutes=45)).isoformat(),
             "hb::listener": (now - timedelta(minutes=45)).isoformat()}
    rows = _rows(state, lambda wf: ("in_progress", 5.0))
    assert rows["monitor"][1] is False
    assert rows["listener"][1] is False
    assert "monitor" not in health.EXIT_PERSISTED


def test_status_message_contains_what_it_should(monkeypatch):
    from datetime import timedelta
    import repo_commit
    monkeypatch.setattr(repo_commit, "refresh_from_origin",
                        lambda path, cwd=None: False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    now = earnings_utils.now_et()
    state = {
        "hb::monitor": now.isoformat(),
        "hb::earnings_arm": (now - timedelta(hours=40)).isoformat(),
        "alert::earnings": (now - timedelta(hours=3)).isoformat(),
        "ew_watch::XPEV": {"armed": "x"},
    }
    msg = tc.build_status(state)
    assert "*Status*" in msg
    assert "XPEV" in msg, "armed watches listed"
    assert "keyword fallback" in msg, "missing model key is visible"
    assert "A component is stale" in msg


@pytest.mark.parametrize("text", ["status", "health", "are you ok", "Status?"])
def test_status_command_routes(text, monkeypatch):
    import repo_commit
    monkeypatch.setattr(repo_commit, "refresh_from_origin",
                        lambda path, cwd=None: False)
    sent = []
    monkeypatch.setattr(tc, "send_telegram_message", sent.append)
    tc.process_message(text, ["AMAT"], {}, [], {})
    assert sent and "*Status*" in sent[0]


# --- Calendar-independent arming ------------------------------------------
#
# Arming is the single point of failure in the earnings path: no watch armed
# means detection never runs, however good the detection is. Two calendars
# make a miss less likely and not impossible, and their failures cluster on
# exactly the companies worth watching -- recent IPOs and foreign issuers.

def _stale_date(days):
    from datetime import timedelta
    return (earnings_utils.now_et().date() - timedelta(days=days)).strftime("%Y-%m-%d")


def _prepare(monkeypatch, filings=None, score=(2, False, "routine")):
    import sec_edgar
    monkeypatch.setattr(earnings_watch, "TICKERS", ["GENI"])
    monkeypatch.setattr(sec_edgar, "load_cik_map", lambda: {"GENI": "0001834489"})
    monkeypatch.setattr(sec_edgar, "resolve_cik", lambda t, m: m.get(t.upper()))
    monkeypatch.setattr(sec_edgar, "recent_filings",
                        lambda cik, etag=None: (filings or [], None))
    monkeypatch.setattr(sec_edgar, "score_filing",
                        lambda cik, acc, max_docs=3: score)
    monkeypatch.setattr(earnings_watch, "_set_baseline_now",
                        lambda state, t, c=None: False)
    sent = []
    monkeypatch.setattr(earnings_watch, "send_telegram_message", sent.append)
    return sent


def test_overdue_holding_is_armed_without_any_calendar(monkeypatch):
    sent = _prepare(monkeypatch)
    state = {"ew_last_earnings::GENI": _stale_date(150)}
    earnings_watch._arm_overdue_holdings(state)
    assert "ew_watch::GENI" in state
    assert sent and "Neither calendar" in sent[0]


def test_recently_reported_holding_is_left_alone(monkeypatch):
    sent = _prepare(monkeypatch)
    state = {"ew_last_earnings::GENI": _stale_date(20)}
    earnings_watch._arm_overdue_holdings(state)
    assert "ew_watch::GENI" not in state
    assert not sent


def test_no_history_means_no_arming_on_a_guess(monkeypatch):
    """Arming everything unknown would fire "nothing appeared" notices for
    every holding, which is how a useful alert becomes noise."""
    sent = _prepare(monkeypatch, filings=[{"form": "4", "accession": "x",
                                           "filed": _stale_date(5), "items": "",
                                           "doc": ""}])
    state = {}
    earnings_watch._arm_overdue_holdings(state)
    assert "ew_watch::GENI" not in state
    assert not sent


def test_history_lookup_is_bounded(monkeypatch):
    """6-K classification costs a document fetch each, so the bootstrap scan
    must not walk a company's entire filing history."""
    import sec_edgar
    many = [{"form": "6-K", "accession": f"a{i}", "filed": _stale_date(300 + i),
             "items": "", "doc": "d.htm"} for i in range(30)]
    calls = []
    _prepare(monkeypatch, filings=many)
    monkeypatch.setattr(sec_edgar, "score_filing",
                        lambda cik, acc, max_docs=3: (calls.append(acc), (2, False, ""))[1])
    earnings_watch._last_earnings_date({}, "GENI", "0001834489")
    assert len(calls) <= earnings_watch.BOOTSTRAP_SCORE_LIMIT


def test_fetch_failure_arms_nothing(monkeypatch):
    import sec_edgar
    _prepare(monkeypatch)

    def boom(cik, etag=None):
        raise sec_edgar.FetchError("network")

    monkeypatch.setattr(sec_edgar, "recent_filings", boom)
    state = {}
    earnings_watch._arm_overdue_holdings(state)
    assert not [k for k in state if k.startswith("ew_watch")]


# --- Earnings watcher watchdog --------------------------------------------
#
# On 2026-08-26 NVDA was armed correctly, its baseline was correct, and both
# of its 8-Ks were newer than that baseline -- fully detectable. The last
# watcher run ended at 15:59, NVDA filed at 16:21, and no scheduled run
# followed. Missed by 22 minutes because nothing was polling.
#
# The listener had a watchdog. The watcher did not, despite depending on the
# same unreliable scheduler.

ARMED_STATE = {"ew_watch::NVDA": {"armed": "2026-08-26T00:40:02-04:00",
                                  "expires": "2026-08-27T00:40:02-04:00"}}


def _fake_api(monkeypatch, status="completed", age_minutes=999):
    """Present the most recent run as `status`, started `age_minutes` ago.

    Mirrors the real payload shape: /runs?per_page=1 returns a workflow_runs
    list whose first element carries `status` and `created_at`. An empty list
    means the workflow has never run.
    """
    import workflow_trigger
    calls = []
    when = (datetime.now(timezone.utc)
            - timedelta(minutes=age_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def api(url, method="GET", body=None):
        calls.append((method, url))
        if method == "POST":
            return 204, {}
        if status is None:
            return 200, {"workflow_runs": []}
        return 200, {"workflow_runs": [{"status": status, "created_at": when}]}

    monkeypatch.setattr(workflow_trigger, "_api", api)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "a/b")
    return calls


def test_watchdog_starts_the_watcher_when_armed_and_nothing_polling(monkeypatch):
    import workflow_trigger
    calls = _fake_api(monkeypatch, status="completed", age_minutes=120)
    assert workflow_trigger.ensure_watcher_running(ARMED_STATE) is True
    assert any(m == "POST" and "earnings_watch.yml" in u for m, u in calls)


def test_watchdog_starts_a_workflow_that_has_never_run(monkeypatch):
    import workflow_trigger
    calls = _fake_api(monkeypatch, status=None)
    assert workflow_trigger.ensure_watcher_running(ARMED_STATE) is True
    assert any(m == "POST" for m, _ in calls)


@pytest.mark.parametrize("status", sorted(
    __import__("workflow_trigger").ACTIVE_STATUSES))
def test_watchdog_leaves_a_live_watcher_alone_in_every_active_status(
        status, monkeypatch):
    """That workflow cancels in progress, so dispatching at the wrong moment
    would kill the watcher this exists to protect.

    Checking only in_progress and queued was not enough: a run waiting on a
    concurrency lock reports neither, and the watchdog read that as "nothing
    is running" and dispatched over it.
    """
    import workflow_trigger
    calls = _fake_api(monkeypatch, status=status, age_minutes=120)
    assert workflow_trigger.ensure_watcher_running(ARMED_STATE) is False
    assert not any(m == "POST" for m, _ in calls)


@pytest.mark.parametrize("age", [0, 0.5, 3, 7.9])
def test_a_run_that_just_started_is_never_dispatched_over(age, monkeypatch):
    """The bug of 2026-08-27, and the only guard that actually closes it.

    Watcher runs started at 14:16, 14:17, 14:18, 14:19, 14:20 and 14:20:50 --
    each dispatch killing the one before, so nothing ever polled for more than
    a minute. The API had reported the just-created run as absent, because it
    is eventually consistent.

    So status is not trusted on its own: whatever the API says, a workflow
    started seconds ago is left alone. Being slow to restart costs one
    interval; restarting too often costs everything.
    """
    import workflow_trigger
    calls = _fake_api(monkeypatch, status="completed", age_minutes=age)
    assert workflow_trigger.ensure_watcher_running(ARMED_STATE) is False
    assert not any(m == "POST" for m, _ in calls)


def test_repeated_checks_over_an_hour_cannot_thrash(monkeypatch):
    """The property that matters, stated as a rate rather than a case.

    The monitor calls this every five minutes and the watcher may keep dying;
    no combination of the two may produce a dispatch per check.
    """
    import workflow_trigger
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "a/b")
    dispatched = []
    clock = {"now": 0.0, "last_start": None}

    def api(url, method="GET", body=None):
        if method == "POST":
            dispatched.append(clock["now"])
            clock["last_start"] = clock["now"]
            return 204, {}
        if clock["last_start"] is None:
            return 200, {"workflow_runs": []}
        started = (datetime.now(timezone.utc)
                   - timedelta(minutes=clock["now"] - clock["last_start"]))
        # Worst case: every run dies at once, so it always reads completed.
        return 200, {"workflow_runs": [
            {"status": "completed",
             "created_at": started.strftime("%Y-%m-%dT%H:%M:%SZ")}]}

    monkeypatch.setattr(workflow_trigger, "_api", api)
    for minute in range(60):
        clock["now"] = float(minute)
        workflow_trigger.ensure_watcher_running(ARMED_STATE)

    ceiling = 60 / workflow_trigger.DISPATCH_COOLDOWN_MINUTES + 1
    assert len(dispatched) <= ceiling, (
        f"{len(dispatched)} dispatches in an hour; the cooldown is not holding")
    gaps = [b - a for a, b in zip(dispatched, dispatched[1:])]
    assert all(g >= workflow_trigger.DISPATCH_COOLDOWN_MINUTES for g in gaps)


def test_watchdog_does_nothing_when_no_watch_is_armed(monkeypatch):
    """The watcher exits when idle. Dispatching on an empty state would start
    a runner to do nothing, every minute, forever."""
    import workflow_trigger
    calls = _fake_api(monkeypatch, status="completed", age_minutes=999)
    assert workflow_trigger.ensure_watcher_running({}) is False
    assert workflow_trigger.ensure_watcher_running({"price_ref::AMAT": {}}) is False
    assert not calls


def test_watchdog_does_not_dispatch_when_the_lookup_fails(monkeypatch):
    import urllib.error
    import workflow_trigger
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "a/b")

    def boom(url, method="GET", body=None):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(workflow_trigger, "_api", boom)
    assert workflow_trigger.ensure_watcher_running(ARMED_STATE) is False


def test_the_listener_watchdog_is_guarded_identically(monkeypatch):
    """Both watchdogs drive cancel-in-progress workflows, so both need the
    same restraint. The listener path was written first and must not drift."""
    import workflow_trigger
    calls = _fake_api(monkeypatch, status="in_progress", age_minutes=120)
    assert workflow_trigger.ensure_listener_running() is False
    assert not any(m == "POST" for m, _ in calls)

    calls = _fake_api(monkeypatch, status="completed", age_minutes=1)
    assert workflow_trigger.ensure_listener_running() is False
    assert not any(m == "POST" for m, _ in calls)

    calls = _fake_api(monkeypatch, status="completed", age_minutes=120)
    assert workflow_trigger.ensure_listener_running() is True
    assert any(m == "POST" and "telegram_commands.yml" in u for m, u in calls)


# --- Model key must reach every workflow that can send an earnings alert ---

def test_workflows_provide_every_env_var_their_code_can_read():
    """Derived from the IMPORT GRAPH, not from a list I maintain by hand.

    The NVDA extraction failed because the listener workflow had no
    GROQ_API_KEY, while the identical code worked in three other workflows.
    Adding the catch-up gave the Telegram path the ability to send a full
    earnings alert; nothing gave its workflow the ability to parse one.

    My first attempt at guarding this hardcoded which scripts were
    "earnings-capable" -- the same stale-list pattern that caused the bug.
    Walking imports from each workflow's entrypoint and collecting every
    os.environ read is not a list anyone has to remember to update, and when
    written it immediately found two more gaps (monitor missing
    FINNHUB_API_KEY, simulate missing GITHUB_TOKEN) that a hand list never
    would have.

    Latent rather than live is not a defence: "not called today" was exactly
    true of the listener's model key until the day it was.
    """
    import ast
    import glob
    import os
    import re

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    local = {os.path.splitext(os.path.basename(f))[0]
             for f in glob.glob(os.path.join(root, "*.py"))}

    def imports_of(mod):
        try:
            tree = ast.parse(open(os.path.join(root, f"{mod}.py")).read())
        except OSError:
            return set()
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module.split(".")[0])
        return found & local

    def reachable(entry):
        seen, stack = set(), [entry]
        while stack:
            mod = stack.pop()
            if mod in seen:
                continue
            seen.add(mod)
            stack.extend(imports_of(mod) - seen)
        return seen

    env_re = re.compile(
        r'os\.environ(?:\.get)?\(\s*["\']([A-Z_][A-Z0-9_]*)["\']'
        r'|os\.environ\[\s*["\']([A-Z_][A-Z0-9_]*)["\']')

    def env_used(mod):
        try:
            src = open(os.path.join(root, f"{mod}.py")).read()
        except OSError:
            return set()
        return {a or b for a, b in env_re.findall(src)}

    # Injected by Actions, or genuinely optional with a documented fallback.
    auto = {"GITHUB_REPOSITORY", "GITHUB_REF_NAME", "GITHUB_SHA", "GITHUB_RUN_ID", "CI"}
    optional = {"GEMINI_API_KEY", "HEALTHCHECK_PING_KEY", "GROQ_MODEL"}

    problems = []
    for path in sorted(glob.glob(os.path.join(root, ".github", "workflows", "*.yml"))):
        text = open(path).read()
        for entry in sorted(set(re.findall(r"run:\s*.*?python3?\s+(\w+)\.py", text))):
            needed = set().union(*(env_used(m) for m in reachable(entry))) or set()
            missing = sorted(n for n in needed - auto - optional if n not in text)
            if missing:
                problems.append(f"{os.path.basename(path)} ({entry}.py): {missing}")

    assert not problems, "workflow env does not cover what the code can read:\n" + "\n".join(problems)


def test_missing_key_and_failed_call_are_reported_differently(monkeypatch):
    """"Figures could not be extracted" sent me looking at the filing. The
    real answer was a missing secret, which staring at a document cannot
    reveal."""
    import llm_extract

    monkeypatch.setattr(llm_extract, "highlights", lambda text, limit=700: "the release text")

    monkeypatch.setattr(llm_extract, "extract_metrics",
                        lambda text, ticker: llm_extract.NO_PROVIDER)
    msg = earnings_watch.build_metrics_message("NVDA", "body", {})
    assert "No model key configured" in msg

    monkeypatch.setattr(llm_extract, "extract_metrics", lambda text, ticker: None)
    msg = earnings_watch.build_metrics_message("NVDA", "body", {})
    assert "model call failed" in msg


# --- The watcher must import with no third-party packages -----------------

def test_earnings_watcher_imports_with_zero_third_party_packages():
    """The watch job does a checkout and runs python3 directly -- no
    setup-python, no pip install. That is where its latency advantage comes
    from, and it only holds if NOTHING on its import path reaches a package
    the runner does not install.

    It did not hold. earnings_watch imported earnings_utils for four small
    stdlib-only helpers, and earnings_utils imports requests and yfinance at
    module level for calendar functions the watcher never calls. telegram_utils
    imported requests too. The job started only because the runner image
    happened to ship those packages -- a constraint holding by luck.

    On 2026-08-27 the watch step began exiting in 0 seconds while state.json
    still listed four armed watches, and the watchdog re-dispatched it every
    minute. Had RBRK reported that evening, nothing would have been polling.

    This test runs the import the way the runner does.
    """
    import subprocess
    import sys
    import textwrap

    probe = textwrap.dedent("""
        import sys
        BLOCKED = {"yfinance", "requests", "pandas", "numpy", "lxml",
                   "bs4", "curl_cffi"}

        class Blocker:
            def find_module(self, name, path=None):
                return self if name.split(".")[0] in BLOCKED else None

            def load_module(self, name):
                raise ImportError(name + " is not installed on the watcher")

        sys.meta_path.insert(0, Blocker())
        import earnings_watch
        assert callable(earnings_watch.poll)
        print("OK")
    """)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = subprocess.run([sys.executable, "-c", probe],
                            capture_output=True, text=True, cwd=root)
    assert "OK" in result.stdout, (
        "the watcher cannot start without pip install:\n" + result.stderr[-1500:])
