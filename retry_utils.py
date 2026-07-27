"""Retry/backoff helpers shared by every external API call in this project.

The previous version of this pipeline wrapped each eBay fetch in a single bare
`except Exception`, which treated a transient network blip exactly the same as
"item permanently removed" - silently zeroing a live listing's price and stock
on an ordinary timeout. These helpers separate retryable failures (network
errors, 5xx, 429) from real, permanent ones (bad credentials, 4xx, item gone)
so only genuine "the item is gone" signals ever zero out a listing.
"""
import logging
import random
import time

import requests

logger = logging.getLogger("onbuy_sync")


class TransientError(Exception):
    """Retryable: network errors, timeouts, 5xx responses."""


class RateLimitError(TransientError):
    """Retryable, but wait for the server-declared cooldown instead of guessing."""

    def __init__(self, retry_after=None):
        super().__init__(f"rate limited (retry_after={retry_after})")
        self.retry_after = retry_after


class PermanentError(Exception):
    """Not retryable: 4xx validation errors, malformed payloads."""


class AuthError(PermanentError):
    """Credentials are invalid/expired. Callers must abort the whole run/account,
    not just this one item - this is what stops a bad token from cascading
    through an entire batch the way it used to."""


def raise_for_status(response: requests.Response, what: str = "request") -> None:
    """Classify an HTTP response into the exception hierarchy above. No-op on 2xx."""
    if response.status_code < 400:
        return
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        raise RateLimitError(retry_after=int(retry_after) if retry_after and retry_after.isdigit() else None)
    if response.status_code in (401, 403):
        raise AuthError(f"{what}: auth failed ({response.status_code}): {response.text[:300]}")
    if 500 <= response.status_code < 600:
        raise TransientError(f"{what}: server error {response.status_code}: {response.text[:300]}")
    raise PermanentError(f"{what}: client error {response.status_code}: {response.text[:300]}")


def with_retry(fn, *args, what="request", max_attempts=4, base_delay=2.0, max_delay=60.0, **kwargs):
    """Call fn(*args, **kwargs), retrying transient/rate-limit/network failures
    with exponential backoff + jitter. PermanentError (including AuthError) is
    raised immediately without retrying - retrying a bad password or a malformed
    payload wastes time and can make rate limiting worse.
    """
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn(*args, **kwargs)
        except RateLimitError as exc:
            last_exc = exc
            if attempt >= max_attempts:
                break
            delay = exc.retry_after if exc.retry_after else min(base_delay * (2 ** (attempt - 1)), max_delay)
            logger.warning("%s: rate limited, retrying in %.0fs (attempt %d/%d)", what, delay, attempt, max_attempts)
            time.sleep(delay)
        except TransientError as exc:
            last_exc = exc
            if attempt >= max_attempts:
                break
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay) + random.uniform(0, 1)
            logger.warning("%s: transient error (%s), retrying in %.1fs (attempt %d/%d)", what, exc, delay, attempt, max_attempts)
            time.sleep(delay)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_exc = exc
            if attempt >= max_attempts:
                break
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay) + random.uniform(0, 1)
            logger.warning("%s: network error (%s), retrying in %.1fs (attempt %d/%d)", what, exc, delay, attempt, max_attempts)
            time.sleep(delay)
    raise last_exc
