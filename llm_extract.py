"""Pulls structured metrics out of an earnings release.

Why a model rather than more regexes
------------------------------------
The previous approach matched four patterns and took the first hit. That is
unsafe on real releases, because an earnings press release states the same
metric several times over: current quarter, year-ago quarter, six months
ended, GAAP beside non-GAAP, and again inside HTML tables. A pattern like
`revenue of \\$X` frequently matches the YEAR-AGO figure, and there is no
signal that it did -- the alert simply carries a wrong number that looks
right.

For a figure you might act on, a confidently wrong revenue is worse than no
revenue. Extending regexes to ten metrics multiplies that risk tenfold while
keeping the failure silent.

A model reads the surrounding sentence and gets the period and the GAAP /
non-GAAP distinction right. At roughly 36 earnings events a year this fits
inside any free tier several times over.

Design constraints
------------------
Standard library only, so the watcher still needs no pip install and keeps
its ~25-55 second startup saving.

Failure is never fatal. The stage-one alert -- the fact of the filing and its
link -- has already been sent by the time this runs. If the model is down,
rate-limited or returns nonsense, the caller falls back to quoting the
company's own highlights verbatim, which needs no parsing at all.

Providers
---------
Whichever key is present, checked in order. Both have free tiers that cover
this volume many times over:

    GROQ_API_KEY     fastest, ~1s round trip
    GEMINI_API_KEY   generous free tier
"""

import json
import os
import re
import urllib.error

import llm_client

# Releases run to hundreds of KB, mostly financial-statement tables. The
# headline figures, the year-over-year comparisons and the guidance all live
# near the top, so the opening is where the signal is -- and a smaller
# payload is faster and cheaper.
MAX_CHARS = 18000

TIMEOUT = 20

_SCHEMA = """{
  "period": "e.g. Q2 2026, or null",
  "revenue": "as written, e.g. $1.24 billion, or null",
  "revenue_yoy": "e.g. +18%, or null",
  "eps_gaap": "GAAP/reported diluted EPS, or null",
  "eps_adjusted": "non-GAAP/adjusted diluted EPS, or null",
  "net_income": "net income or loss, or null",
  "gross_margin": "or null",
  "operating_income": "or null",
  "operating_margin": "or null",
  "adjusted_ebitda": "or null",
  "free_cash_flow": "or null",
  "guidance": "forward guidance in one sentence, or null",
  "headline": "the single most important fact, one short sentence"
}"""

# Built by substitution, NOT by %-formatting.
#
# The document being inserted is an earnings release, and earnings releases
# are made of percentages. "Revenue increased 12%, driven by..." makes
# `PROMPT % (ticker, text)` raise "unsupported format character ','" -- so
# extraction did not degrade on unusual documents, it crashed on ordinary
# ones. Almost every release in existence contains a percent sign.
#
# Never %-format or .format() a template with untrusted text in it. The
# text decides whether your format string is valid.
PROMPT_TEMPLATE = """You are extracting figures from a company earnings press release.

Rules:
- Report ONLY the CURRENT reporting period. Releases also state the year-ago
  quarter and year-to-date totals; do not confuse those for the current one.
- Keep GAAP and non-GAAP (adjusted) EPS separate. Never merge them.
- Copy values as written, including units and currency.
- Use null for anything not stated. Do not infer, estimate or calculate.
- Return ONLY a JSON object, no prose, no code fences.

Schema:
<<SCHEMA>>

Release for <<TICKER>>:
---
<<RELEASE>>
---"""


def build_prompt(ticker: str, text: str) -> str:
    return (PROMPT_TEMPLATE
            .replace("<<SCHEMA>>", _SCHEMA)
            .replace("<<TICKER>>", ticker)
            .replace("<<RELEASE>>", text[:MAX_CHARS]))

_FIELDS = ("period", "revenue", "revenue_yoy", "eps_gaap", "eps_adjusted",
           "net_income", "gross_margin", "operating_income",
           "operating_margin", "adjusted_ebitda", "free_cash_flow",
           "guidance", "headline")


def _parse(raw):
    """Models occasionally wrap JSON in code fences despite being told not to."""
    raw = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    if fence:
        raw = fence.group(1).strip()
    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise ValueError("expected a JSON object")
    # Normalise: keep only known fields, drop nulls and empty strings so the
    # caller can simply test truthiness.
    out = {}
    for field in _FIELDS:
        value = obj.get(field)
        if value in (None, "", "null", "N/A"):
            continue
        out[field] = str(value).strip()
    return out


# Distinguishable from None so the alert can say WHICH thing went wrong.
# "Figures could not be extracted" sent you looking at a document; the real
# answer was a missing secret in one workflow, which no amount of staring at
# the filing would have revealed.
NO_PROVIDER = "no-provider"


def extract_metrics(text: str, ticker: str):
    """Returns a dict of figures, or None if no provider is configured or the
    call fails. None means "fall back to quoting the release", never "there
    were no earnings"."""
    providers = llm_client.providers()
    if not providers:
        print("No GROQ_API_KEY or GEMINI_API_KEY; using verbatim highlights.")
        return NO_PROVIDER

    prompt = build_prompt(ticker, text)
    for name, call, key in providers:
        try:
            result = _parse(call(prompt, key))
            if result:
                print(f"{name}: extracted {len(result)} fields.")
                return result
            print(f"{name}: returned nothing usable.")
        except urllib.error.HTTPError as e:
            print(f"{name}: HTTP {e.code} {e.read()[:150]!r}")
        except Exception as e:
            print(f"{name}: {type(e).__name__}: {e}")
    return None


# Abbreviations whose full stop does not end a sentence. Without these,
# "XPeng Inc. today announced..." splits mid-name and "Aug. 24, 2026" splits
# a date -- which is worse than not paragraphing at all, because it reads as
# though the text itself is broken.
_ABBREV = (
    "Inc", "Corp", "Co", "Ltd", "LLC", "plc", "PLC", "AG", "SA", "NV", "AB",
    "Mr", "Mrs", "Ms", "Dr", "Jr", "Sr", "St",
    "Jan", "Feb", "Mar", "Apr", "Jun", "Jul", "Aug", "Sept", "Sep", "Oct",
    "Nov", "Dec",
    "vs", "etc", "approx", "est", "No", "Nos", "Fig", "p", "pp",
)
_SENTINEL = "\x00"
_ABBREV_RE = re.compile(r"\b(" + "|".join(_ABBREV) + r")\.", re.IGNORECASE)

# A real boundary: terminator, whitespace, then something that starts a
# sentence. The uppercase lookahead also keeps decimals ("17.3%") intact.
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(\u201c])")


def _paragraph(text: str, per_para: int = 2) -> str:
    """Group sentences into short paragraphs.

    A press release opening arrives as one unbroken wall of prose. On a phone
    that is genuinely hard to read, and this fallback is exactly what you get
    when the model is unavailable -- which is when readability matters most,
    because it is all there is.
    """
    protected = _ABBREV_RE.sub(lambda m: m.group(1) + _SENTINEL, text.strip())
    sentences = [s for s in _SENTENCE_RE.split(protected) if s.strip()]

    paragraphs, chunk = [], []
    for sentence in sentences:
        chunk.append(sentence.strip())
        if len(chunk) >= per_para:
            paragraphs.append(" ".join(chunk))
            chunk = []
    if chunk:
        paragraphs.append(" ".join(chunk))
    return "\n\n".join(paragraphs).replace(_SENTINEL, ".")


def highlights(text: str, limit: int = 700) -> str:
    """The release's own opening, verbatim, broken into paragraphs.

    Used when no model is available and printed alongside the extracted
    figures when one is, so there is always an unparsed version to check
    against. Companies lead with their headline numbers, so the first few
    hundred characters after the boilerplate usually carry them.
    """
    # EDGAR repeats the exhibit marker in its header: "EX-99.1 2 amat.htm
    # EXHIBIT 99.1 Exhibit 99.1 <actual text>". A non-greedy strip stops at
    # the first one and leaves the rest of the header in the quote, so take
    # the LAST marker -- but only look in the first 400 characters, since a
    # greedy search over the whole document would happily skip past real
    # content to a cross-reference further down.
    head = text[:400]
    markers = list(re.finditer(r"(?:Exhibit\s+99\.?\d*|EX-99\.?\d*)",
                               head, re.IGNORECASE))
    start = markers[-1].end() if markers else 0
    body = text[start:start + 4000].strip() or text.strip()
    if len(body) > limit:
        cut = body[:limit]
        stop = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        body = (cut[:stop + 1] if stop > limit // 2 else cut).strip() + " ..."
    return _paragraph(body)


def compare_to_consensus(metrics: dict, consensus: dict):
    """Beat/miss on EPS, when both a reported figure and an estimate exist.

    Deliberately EPS only. Revenue estimates and reported revenue are quoted
    on different bases often enough (constant-currency, segment splits,
    net-vs-gross) that a mechanical comparison would mislead more than it
    informs.

    Adjusted EPS is preferred when present, since that is what sell-side
    estimates are almost always stated against.
    """
    if not metrics or not consensus:
        return None
    estimate = consensus.get("eps_estimate")
    reported = metrics.get("eps_adjusted") or metrics.get("eps_gaap")
    if estimate in (None, "") or not reported:
        return None

    number = re.search(r"-?\$?\(?\s*(\d+(?:\.\d+)?)\s*\)?", str(reported))
    if not number:
        return None
    value = float(number.group(1))
    if "(" in str(reported) or str(reported).strip().startswith("-"):
        value = -value

    try:
        estimate = float(estimate)
    except (TypeError, ValueError):
        return None

    delta = value - estimate
    basis = "adj." if metrics.get("eps_adjusted") else "GAAP"
    verdict = "beat" if delta > 0 else ("missed" if delta < 0 else "in line")
    return (f"EPS {reported} ({basis}) vs {estimate:g} expected "
            f"— {verdict} by {abs(delta):.2f}" if delta else
            f"EPS {reported} ({basis}) vs {estimate:g} expected — in line")
