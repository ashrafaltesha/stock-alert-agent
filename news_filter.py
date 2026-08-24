"""Decides which news is worth interrupting you for.

What this replaces
------------------
A substring match against ~40 keywords, applied to the headline:

    return any(keyword in title.lower() for keyword in MATERIAL_NEWS_KEYWORDS)

That fails in both directions, and the false positives are the worse half.
Content farms write headlines stuffed with exactly those words because it is
how they rank, so "5 AI stocks to watch as NVDA guidance comes into focus"
matches on `guidance` and "Why Wolfspeed could benefit from any partnership
news" matches on `partnership`. Neither contains a fact. Meanwhile anything
phrased outside the list is missed entirely -- the same brittleness that
nearly lost the Cerebras earnings release.

The list also has no notion of whether an article is ABOUT the company or
merely mentions it among ten others.

How it works instead
--------------------
Three stages, cheapest first:

1. SOURCE. Drop known aggregators and listicle farms outright. This is the
   single highest-yield filter and it costs nothing -- most noise comes from
   a small set of publishers.

2. CLASSIFY. One model call per run, batched across every candidate article,
   answering three things per item: is the company the primary subject, what
   kind of event is it, and would a reasonable holder act on it.

3. THRESHOLD. Only high-materiality, company-specific items are sent.
   Everything else is logged and dropped silently.

Batching matters. One call per run rather than per article keeps this inside
a free tier comfortably: new articles are rare on any given minute, so this
amounts to a handful of calls a day.

Degradation
-----------
With no model key the keyword filter is used as before. That is worse, but it
is the status quo rather than a regression, and a news alert is not worth
failing a run over.
"""

import json
import os
import re
import urllib.error
import urllib.request

from config import MATERIAL_NEWS_KEYWORDS

TIMEOUT = 25

# Publishers whose output is overwhelmingly listicles, recycled wire copy or
# SEO filler. Dropping these before any analysis removes most of the noise for
# free -- no model call, no latency.
#
# This blocks the SOURCE, not the story: a genuine event covered by these
# outlets is invariably covered by the wires and the majors too, and that copy
# still gets through.
# Both spaced and domain spellings are listed on purpose. Google News puts
# the publisher's own display name in <source>, and for several of these that
# is the domain rather than the prose name -- the alert history shows
# "simplywall.st", not "Simply Wall St". Matching only the spaced form let
# every one of that publisher's articles through.
BLOCKED_SOURCES = (
    "zacks",
    "motley fool", "fool.com",
    "simply wall st", "simplywall.st", "simplywallst",
    "insider monkey", "insidermonkey",
    "investorplace",
    "24/7 wall st", "247wallst",
    "stocktwits",
    "benzinga insights",
    "tipranks",
    "seeking alpha - all",
    "marketbeat",
    "gurufocus",
    "wall street zen", "wallstreetzen",
    "blockonomi",
    "invezz", "the tokenist", "finbold", "watcher.guru",
)

# Headline shapes that are never a company event, whoever published them.
# Every pattern here was taken from an alert this bot actually sent.
_NOISE_RE = re.compile(
    # Listicles and opinion framing.
    r"^\s*\d+\s+(?:best|top|great|cheap|growth|dividend|ai|stocks?)\b|"
    r"\b(?:stocks? to (?:watch|buy|sell)|things to know|what to know|"
    r"here'?s why|is it time to|should you buy|better buy|motley fool|"
    r"jim cramer|price prediction|forecast for 20\d\d)\b|"
    # Institutional ownership churn. A pension fund rebalancing its book is a
    # regulatory filing, not news about the company.
    r"\b(?:shares? (?:acquired|sold|bought|purchased) by|"
    r"(?:acquires|buys|sells|takes) (?:new )?(?:shares|stake|position)|"
    r"(?:position|stake|holdings?) (?:boosted|lowered|trimmed|raised|cut) by|"
    r"13f|sells shares of)\b|"
    # Single-bank price-target tweaks. These were the largest single category
    # of false alert in the history -- six of thirteen -- and a broker moving
    # its target from $600 to $650 is not something you act on.
    r"\b(?:price target|target price)\b.{0,40}\b(?:to \$|from \$|cut|"
    r"raised|lowered|adjust\w*|maintain\w*|set\w*)\b|"
    r"\badjusts? (?:its )?(?:price )?target\b|"
    r"\b\d+.?month price target\b|"
    # Valuation opinion with no event behind it.
    r"\b(?:under|over)valued\b|\b(?:fair|intrinsic) value\b",
    re.IGNORECASE)

_SCHEMA = """[
  {"i": 0, "subject": true|false, "event": "short label", "impact": "high"|"medium"|"low", "why": "one short clause"}
]"""

# Substitution, not %-formatting: headlines are full of percent signs
# ("AppLovin trading down 4.7% on analyst downgrade" is a real one from this
# bot's own alert history) and `PROMPT % (...)` raises on them. A stray "%s"
# or "%," in a headline would have taken down the whole run's classification.
PROMPT_TEMPLATE = """You screen financial news for someone who HOLDS these stocks.

For each numbered headline decide:
- subject: is this company the PRIMARY subject? false if it is one of several
  mentioned, or if the article is really about a sector, an index or a rival.
- event: a short label, e.g. M&A, guidance, contract, regulatory, litigation,
  analyst action, capital raise, product, management change, earnings.
- impact: "high" only if a reasonable holder would plausibly act on it or
  reassess the position today. Never high: opinion, speculation, recycled
  analysis, general market commentary, a single broker changing its price
  target or rating, institutional ownership filings, and "stock moved X%
  today" stories that only restate the price.
- why: one short clause stating the actual fact. No hedging.

Return ONLY a JSON array, one object per headline, no prose, no code fences.

Schema:
<<SCHEMA>>

Headlines:
<<HEADLINES>>"""


def build_prompt(listing: str) -> str:
    return (PROMPT_TEMPLATE
            .replace("<<SCHEMA>>", _SCHEMA)
            .replace("<<HEADLINES>>", listing))


# Groq sits behind Cloudflare, which rejects urllib's default
# "Python-urllib/3.x" signature with HTTP 403 and "error code: 1010" -- a
# browser-signature ban, not a bad key or a rate limit. Sending ordinary
# request headers is the whole fix.
_BASE_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; personal-stock-alerts/1.0)",
}


def _post(url, payload, headers):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={**_BASE_HEADERS, **headers})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _call_groq(prompt, key):
    data = _post("https://api.groq.com/openai/v1/chat/completions",
                 {"model": "llama-3.3-70b-versatile",
                  "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0,
                  "response_format": {"type": "json_object"}},
                 {"Authorization": f"Bearer {key}"})
    return data["choices"][0]["message"]["content"]


def _call_gemini(prompt, key):
    data = _post("https://generativelanguage.googleapis.com/v1beta/models/"
                 f"gemini-2.0-flash:generateContent?key={key}",
                 {"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"temperature": 0,
                                       "responseMimeType": "application/json"}},
                 {})
    return data["candidates"][0]["content"]["parts"][0]["text"]


def _parse_array(raw):
    """Models asked for JSON sometimes wrap the array in an object, or in
    code fences. Accept the common shapes rather than failing the run."""
    raw = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    if fence:
        raw = fence.group(1).strip()
    obj = json.loads(raw)
    if isinstance(obj, dict):
        for value in obj.values():
            if isinstance(value, list):
                obj = value
                break
        else:
            obj = [obj]
    if not isinstance(obj, list):
        raise ValueError("expected a JSON array")
    return obj


def source_allowed(source: str, title: str) -> bool:
    """Cheap pre-filter. No model call, no network."""
    lowered = (source or "").lower()
    if any(bad in lowered for bad in BLOCKED_SOURCES):
        return False
    return not _NOISE_RE.search(title or "")


def _providers():
    out = []
    if os.environ.get("GROQ_API_KEY"):
        out.append(("groq", _call_groq, os.environ["GROQ_API_KEY"]))
    if os.environ.get("GEMINI_API_KEY"):
        out.append(("gemini", _call_gemini, os.environ["GEMINI_API_KEY"]))
    return out


def classify(articles):
    """articles: [{"ticker","title","source"}] -> [verdict or None] aligned by index.

    A verdict is {"subject","event","impact","why"}. None means "no judgement
    available" and the caller falls back to keywords for that item.
    """
    providers = _providers()
    if not providers or not articles:
        return [None] * len(articles)

    listing = "\n".join(
        f'{i}. [{a["ticker"]}] {a["title"]}  (source: {a.get("source") or "?"})'
        for i, a in enumerate(articles))
    prompt = build_prompt(listing)

    for name, call, key in providers:
        try:
            rows = _parse_array(call(prompt, key))
        except urllib.error.HTTPError as e:
            print(f"news/{name}: HTTP {e.code} {e.read()[:150]!r}")
            continue
        except Exception as e:
            print(f"news/{name}: {type(e).__name__}: {e}")
            continue

        # Index by the model's own "i" rather than position: a dropped or
        # reordered row would otherwise attach one article's verdict to
        # another's headline, which is worse than having no verdict at all.
        verdicts = [None] * len(articles)
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                idx = int(row.get("i"))
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(articles):
                verdicts[idx] = {
                    "subject": bool(row.get("subject")),
                    "event": str(row.get("event") or "").strip() or "news",
                    "impact": str(row.get("impact") or "").strip().lower(),
                    "why": str(row.get("why") or "").strip(),
                }
        got = sum(1 for v in verdicts if v)
        print(f"news/{name}: classified {got}/{len(articles)}.")
        if got:
            return verdicts
    return [None] * len(articles)


def keyword_material(title: str) -> bool:
    """The old behaviour, kept only as the no-model fallback."""
    lowered = (title or "").lower()
    return any(k in lowered for k in MATERIAL_NEWS_KEYWORDS)


def should_alert(verdict, title: str):
    """Returns (send, label). Threshold: high impact AND primarily about the
    company.

    Both halves are needed. Impact alone lets through "chip stocks surge on
    AI demand" -- genuinely high-impact news that happens not to be about
    your company. Subject alone lets through every product blog post.
    """
    if verdict is None:
        return keyword_material(title), ""
    if not verdict["subject"]:
        return False, ""
    if verdict["impact"] != "high":
        return False, ""
    label = verdict["event"]
    if verdict["why"]:
        label = f"{label} — {verdict['why']}"
    return True, label
