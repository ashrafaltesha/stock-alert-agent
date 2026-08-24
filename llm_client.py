"""One place for the free LLM providers and their quirks.

Both llm_extract.py and news_filter.py call the same two providers, and until
now each had its own copy of the request code. That meant every provider-side
change had to be found and fixed twice, and both copies carried the same two
bugs at the same time:

  * urllib's default "Python-urllib/3.12" User-Agent, which Cloudflare rejects
    outright with HTTP 403 and "error code: 1010" -- a browser-signature ban,
    easily mistaken for a bad key.
  * a hardcoded model name. Groq deprecated llama-3.3-70b-versatile for free
    and developer tiers on 2026-06-17, and the next call returned 404 "does
    not exist or you do not have access to it". A silently pinned model is a
    scheduled outage.

Model selection
---------------
Preferences are a LIST, not a constant, and an unknown-model error triggers
discovery against the provider's own /models endpoint rather than failure.
So the next deprecation costs one wasted call, not a broken feature nobody
notices until earnings day.

Standard library only: the earnings watcher runs with no pip install.
"""

import json
import os
import urllib.error
import urllib.request

TIMEOUT = 25

# Cloudflare sits in front of Groq and bans urllib's default signature.
BASE_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; personal-stock-alerts/1.0)",
}

# Tried in order. Current Groq production models first, then the retired
# Llama one -- enterprise accounts with committed spend still have access to
# it, so it is worth a try rather than an assumption.
GROQ_MODELS = (
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "llama-3.3-70b-versatile",
)

GEMINI_MODELS = (
    "gemini-2.0-flash",
    "gemini-1.5-flash",
)

GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"

# Resolved once per process, so a long-running watcher pays discovery at most
# once even if it extracts figures for several companies in an hour.
_resolved_groq_model = None


def _post(url, payload, headers=None):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={**BASE_HEADERS, **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _get(url, headers=None):
    req = urllib.request.Request(
        url, headers={**BASE_HEADERS, **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _is_unknown_model(err: urllib.error.HTTPError, body: bytes) -> bool:
    """A model that is gone, as opposed to a key or quota problem.

    Worth distinguishing: an unknown model is fixed by picking another one,
    while a 401 or 429 is not, and retrying every model against a bad key
    just multiplies the failures.
    """
    if err.code not in (400, 404):
        return False
    text = (body or b"").decode("utf-8", "replace").lower()
    return ("does not exist" in text or "model_not_found" in text
            or "decommissioned" in text or "unknown model" in text)


def _discover_groq_model(key: str):
    """Ask Groq what it actually serves, and pick the best available."""
    try:
        data = _get(GROQ_MODELS_URL, {"Authorization": f"Bearer {key}"})
    except Exception as e:
        print(f"groq: model discovery failed: {type(e).__name__}: {e}")
        return None

    available = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
    if not available:
        return None
    for preferred in GROQ_MODELS:
        if preferred in available:
            return preferred
    # Nothing preferred survived. Take any chat model rather than give up;
    # whisper and guard models are not usable here.
    for model_id in available:
        low = model_id.lower()
        if not any(skip in low for skip in ("whisper", "guard", "tts", "embed")):
            print(f"groq: falling back to {model_id}")
            return model_id
    return None


def call_groq(prompt: str, key: str, json_object: bool = True) -> str:
    global _resolved_groq_model

    candidates = ([_resolved_groq_model] if _resolved_groq_model
                  else list(GROQ_MODELS))
    if os.environ.get("GROQ_MODEL"):
        candidates.insert(0, os.environ["GROQ_MODEL"])

    last_error = None
    for model in candidates:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        }
        if json_object:
            payload["response_format"] = {"type": "json_object"}
        try:
            data = _post(GROQ_CHAT_URL, payload,
                         {"Authorization": f"Bearer {key}"})
            _resolved_groq_model = model
            return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            body = e.read()
            if _is_unknown_model(e, body):
                print(f"groq: {model} unavailable, trying the next one.")
                last_error = e
                continue
            raise
    # Every guess was wrong -- ask the provider directly.
    discovered = _discover_groq_model(key)
    if discovered and discovered not in candidates:
        _resolved_groq_model = discovered
        return call_groq(prompt, key, json_object)
    if last_error:
        raise last_error
    raise RuntimeError("groq: no usable model")


def call_gemini(prompt: str, key: str, json_object: bool = True) -> str:
    config = {"temperature": 0}
    if json_object:
        config["responseMimeType"] = "application/json"

    last_error = None
    for model in GEMINI_MODELS:
        url = ("https://generativelanguage.googleapis.com/v1beta/models/"
               f"{model}:generateContent?key={key}")
        try:
            data = _post(url, {"contents": [{"parts": [{"text": prompt}]}],
                               "generationConfig": config})
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except urllib.error.HTTPError as e:
            body = e.read()
            if _is_unknown_model(e, body) or e.code == 404:
                last_error = e
                continue
            raise
    if last_error:
        raise last_error
    raise RuntimeError("gemini: no usable model")


def providers():
    """[(name, callable)] for whichever keys are configured, in order."""
    out = []
    if os.environ.get("GROQ_API_KEY"):
        out.append(("groq", call_groq, os.environ["GROQ_API_KEY"]))
    if os.environ.get("GEMINI_API_KEY"):
        out.append(("gemini", call_gemini, os.environ["GEMINI_API_KEY"]))
    return out
