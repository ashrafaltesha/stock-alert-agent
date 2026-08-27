"""Small helper for sending Telegram messages. STANDARD LIBRARY ONLY.

Uses urllib rather than requests deliberately. The earnings watcher runs with
no `pip install` -- that is where its latency advantage comes from -- and it
sends Telegram messages, so this module is on the watcher's import path. It
imported requests, which meant the watcher only ever started because the
runner image happened to ship that package. A design constraint that holds by
luck is not a constraint.

tests/test_core_logic.py imports earnings_watch with third-party modules
blocked, so this cannot quietly regress.
"""

import json
import urllib.error
import urllib.request

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

TIMEOUT = 15


def _post(url: str, payload: dict):
    """Returns (ok, status, body). Never raises."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json",
        "User-Agent": "personal-stock-alerts/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return True, resp.status, ""
    except urllib.error.HTTPError as e:
        return False, e.code, e.read().decode("utf-8", "replace")[:300]
    except Exception as e:
        return None, None, f"{type(e).__name__}: {e}"

# Characters Telegram's legacy Markdown parser treats as formatting. If an
# external string (a news headline, a publisher name) contains an unbalanced
# one of these, Telegram rejects the WHOLE message with HTTP 400 -- which
# used to mean the alert was silently dropped and never seen.
_MARKDOWN_SPECIALS = ("_", "*", "`", "[")


def escape_markdown(text) -> str:
    """Escape Markdown control characters in text that came from outside
    (headlines, publisher names, anything we didn't compose ourselves).
    Text we format deliberately -- our own *bold* labels -- must NOT be
    passed through this."""
    if not text:
        return ""
    out = str(text)
    for ch in _MARKDOWN_SPECIALS:
        out = out.replace(ch, "\\" + ch)
    return out


def send_telegram_message(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set. "
            "Add them as GitHub Actions secrets."
        )
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }

    ok, status, body = _post(url, payload)
    if ok:
        return
    if ok is None:
        print(f"Telegram send FAILED (network): {body}")
        print(f"  message began: {text[:120]!r}")
        return

    print(f"Telegram send FAILED: HTTP {status} -- {body}")
    print(f"  message began: {text[:120]!r}")

    # A 400 here is almost always a Markdown parse error caused by stray
    # formatting characters in external content. Rather than lose the alert,
    # resend it once as plain text -- worse looking, but delivered.
    if status != 400:
        return

    fallback = dict(payload)
    fallback.pop("parse_mode", None)
    ok, status, body = _post(url, fallback)
    if ok:
        print("  plain-text retry succeeded (formatting stripped).")
    elif ok is None:
        print(f"  plain-text retry FAILED (network): {body}")
    else:
        print(f"  plain-text retry FAILED: HTTP {status} -- {body}")
