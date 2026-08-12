"""Shared network-resilience helpers.

Every external source this bot depends on -- Yahoo Finance (via yfinance),
Google News RSS, Finnhub -- can rate-limit or transiently fail. That got
considerably more likely when the pollers moved from a 5-minute to a
1-minute cadence, which is roughly 5x the request volume against the same
free endpoints.

The failure mode this exists to prevent is a quiet one: previously a
throttled fetch raised, the caller's except block printed a line and
returned, and alerts simply stopped arriving with nothing obviously
broken. Retrying transient failures makes that far less likely, and the
logging here makes it visible in the Actions log when it does happen.
"""

import random
import time

import requests

# Status codes worth retrying: rate limiting plus the usual transient
# server-side failures. Anything else (404, 401, ...) is a real error and
# retrying it just wastes time.
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

DEFAULT_ATTEMPTS = 3


def _backoff(attempt: int, retry_after=None) -> None:
    """Wait before the next attempt. Honours Retry-After when the server
    sends one, otherwise exponential backoff with jitter (~1s, ~2s, ~4s).
    The jitter matters because several tickers are polled in a tight loop
    and we don't want them all retrying in lockstep."""
    if retry_after:
        try:
            time.sleep(min(float(retry_after), 30.0))
            return
        except (TypeError, ValueError):
            pass
    time.sleep((2 ** attempt) + random.uniform(0, 0.5))


def get_with_retry(url, headers=None, params=None, timeout=15,
                   attempts=DEFAULT_ATTEMPTS, label=""):
    """GET with retry on rate limits, 5xx, and transient network errors.

    Returns the Response on success, or None if every attempt failed --
    callers should treat None as "no data this run" and move on rather
    than crashing the whole monitor pass over one bad ticker.
    """
    tag = f"[{label}] " if label else ""
    for attempt in range(attempts):
        is_last = attempt == attempts - 1
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=timeout)
        except Exception as e:
            print(f"{tag}request error (attempt {attempt + 1}/{attempts}): "
                  f"{type(e).__name__}: {e}")
            if is_last:
                return None
            _backoff(attempt)
            continue

        if resp.status_code in RETRY_STATUS:
            print(f"{tag}HTTP {resp.status_code} (attempt {attempt + 1}/{attempts})")
            if is_last:
                print(f"{tag}giving up after {attempts} attempts -- skipping this run.")
                return None
            _backoff(attempt, resp.headers.get("Retry-After"))
            continue

        return resp

    return None


def call_with_retry(fn, attempts=DEFAULT_ATTEMPTS, label=""):
    """Retry a callable that raises on transient failure.

    Used for yfinance, which does its own HTTP internally and raises
    rather than handing back a status code we can inspect. Re-raises the
    final exception so existing caller-side except blocks keep working
    exactly as before -- they just now fire only after real, repeated
    failure instead of a single unlucky request.
    """
    tag = f"[{label}] " if label else ""
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as e:
            print(f"{tag}call failed (attempt {attempt + 1}/{attempts}): "
                  f"{type(e).__name__}: {e}")
            if attempt == attempts - 1:
                raise
            _backoff(attempt)
    return None
