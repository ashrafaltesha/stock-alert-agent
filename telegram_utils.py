"""Small helper for sending Telegram messages."""

import requests
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

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

    try:
        resp = requests.post(url, json=payload, timeout=15)
    except Exception as e:
        print(f"Telegram send FAILED (network): {type(e).__name__}: {e}")
        print(f"  message began: {text[:120]!r}")
        return

    if resp.ok:
        return

    print(f"Telegram send FAILED: HTTP {resp.status_code} -- {resp.text[:300]}")
    print(f"  message began: {text[:120]!r}")

    # A 400 here is almost always a Markdown parse error caused by stray
    # formatting characters in external content. Rather than lose the alert,
    # resend it once as plain text -- worse looking, but delivered.
    if resp.status_code != 400:
        return

    fallback = dict(payload)
    fallback.pop("parse_mode", None)
    try:
        retry = requests.post(url, json=fallback, timeout=15)
    except Exception as e:
        print(f"  plain-text retry FAILED (network): {type(e).__name__}: {e}")
        return

    if retry.ok:
        print("  plain-text retry succeeded (formatting stripped).")
    else:
        print(f"  plain-text retry FAILED: HTTP {retry.status_code} -- {retry.text[:300]}")
