"""Shared HTTP retry helpers for source scanners.

Only throttling responses (HTTP 429) are retried here. Other HTTP statuses are
returned to the caller so each source can preserve its existing handling.
"""

from __future__ import annotations

import email.utils
import time
import threading
from datetime import datetime, timezone

import requests

import scan_control

DEFAULT_BACKOFF = (3, 7, 15)

_RETRY_LOCK = threading.RLock()
_RETRY_STATS = {}

# Shared request pacing/cache for the CivitAI family. CivitAI and CivitAI Red
# scanners run in parallel and can both hit civitai.com, so the limiter must
# live here rather than inside either scanner module.
_PACE_LOCK = threading.RLock()
_PACE_LAST = {}
_PACE_STATS = {}
_CACHE_LOCK = threading.RLock()
_SCAN_CACHE = {}
_CACHE_KEY_LOCKS = {}

def reset_retry_stats():
    with _RETRY_LOCK:
        _RETRY_STATS.clear()
    with _PACE_LOCK:
        _PACE_LAST.clear()
        _PACE_STATS.clear()
    with _CACHE_LOCK:
        _SCAN_CACHE.clear()
        _CACHE_KEY_LOCKS.clear()

def get_retry_stats():
    with _RETRY_LOCK:
        return {k: dict(v) for k, v in _RETRY_STATS.items()}

def get_pacing_stats():
    with _PACE_LOCK:
        return {k: dict(v) for k, v in _PACE_STATS.items()}

def _pace_request(key, min_interval):
    if not key or not min_interval or min_interval <= 0:
        return
    while True:
        with _PACE_LOCK:
            now = time.monotonic()
            last = _PACE_LAST.get(key)
            remaining = 0.0 if last is None else float(min_interval) - (now - last)
            if remaining <= 0:
                _PACE_LAST[key] = now
                item = _PACE_STATS.setdefault(key, {"requests": 0, "wait_seconds": 0.0, "cache_hits": 0})
                item["requests"] += 1
                return
        deadline = time.monotonic() + remaining
        while time.monotonic() < deadline:
            if scan_control.should_stop():
                return
            step = min(0.10, max(0.0, deadline - time.monotonic()))
            if step:
                time.sleep(step)
        with _PACE_LOCK:
            item = _PACE_STATS.setdefault(key, {"requests": 0, "wait_seconds": 0.0, "cache_hits": 0})
            item["wait_seconds"] += max(0.0, remaining)

def _cache_lock_for(key):
    with _CACHE_LOCK:
        lock = _CACHE_KEY_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _CACHE_KEY_LOCKS[key] = lock
        return lock

def get_cached_text_with_backoff(
    session: requests.Session,
    url: str,
    *,
    cache_key,
    provider: str,
    label: str = "request",
    max_retries: int = 3,
    backoff=DEFAULT_BACKOFF,
    pace_key=None,
    min_interval: float = 0.0,
    **kwargs,
):
    """Return ``(status_code, text, cache_hit)`` for scan-local page metadata.

    Per-key locking prevents parallel CivitAI/CivitAI Red workers from fetching
    the same rendered model page twice during one scan. Only successful HTTP
    200 bodies are cached, and ``reset_retry_stats`` clears the cache at the
    beginning of each scan.
    """
    lock = _cache_lock_for(cache_key)
    with lock:
        with _CACHE_LOCK:
            cached = _SCAN_CACHE.get(cache_key)
        if cached is not None:
            if pace_key:
                with _PACE_LOCK:
                    item = _PACE_STATS.setdefault(pace_key, {"requests": 0, "wait_seconds": 0.0, "cache_hits": 0})
                    item["cache_hits"] += 1
            return 200, cached, True

        response = get_with_backoff(
            session, url, provider=provider, label=label, max_retries=max_retries,
            backoff=backoff, pace_key=pace_key, min_interval=min_interval, **kwargs
        )
        text = response.text or ""
        if response.status_code == 200:
            with _CACHE_LOCK:
                _SCAN_CACHE[cache_key] = text
        return response.status_code, text, False

def _record_retry(provider, recovered=False, failed=False):
    with _RETRY_LOCK:
        item = _RETRY_STATS.setdefault(provider, {"retries": 0, "recovered": 0, "failed": 0})
        if not recovered and not failed:
            item["retries"] += 1
        if recovered:
            item["recovered"] += 1
        if failed:
            item["failed"] += 1



def _retry_after_seconds(response, fallback):
    value = (response.headers.get("Retry-After") or "").strip()
    if value:
        try:
            seconds = float(value)
            # Some providers return Retry-After: 0 on subsequent throttled
            # responses. Treat non-positive values as unusable so retries still
            # back off instead of immediately hammering the endpoint again.
            if seconds > 0:
                return seconds
        except ValueError:
            try:
                parsed = email.utils.parsedate_to_datetime(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                now = datetime.now(timezone.utc)
                seconds = (parsed - now).total_seconds()
                if seconds > 0:
                    return seconds
            except Exception:
                pass
    return float(fallback)


def get_with_backoff(
    session: requests.Session,
    url: str,
    *,
    provider: str,
    label: str = "request",
    max_retries: int = 3,
    backoff=DEFAULT_BACKOFF,
    pace_key=None,
    min_interval: float = 0.0,
    **kwargs,
):
    """GET a URL and retry HTTP 429 with Retry-After/exponential fallback.

    Returns the final ``requests.Response``. If all retries are exhausted the
    final 429 response is returned so the source scanner can decide how to
    report or abort that individual search.
    """
    attempts = 0

    while True:
        _pace_request(pace_key, min_interval)
        response = session.get(url, **kwargs)
        if response.status_code != 429:
            if attempts:
                _record_retry(provider, recovered=True)
            return response

        if attempts >= max_retries:
            _record_retry(provider, failed=True)
            print(f"{provider} rate limit persisted after {max_retries} retries: {label}")
            return response

        fallback = backoff[min(attempts, len(backoff) - 1)] if backoff else 3
        wait_seconds = _retry_after_seconds(response, fallback)
        attempts += 1
        _record_retry(provider)

        # Keep logs useful but compact. Avoid printing response bodies because
        # Cloudflare/provider pages can be very large.
        shown_wait = int(wait_seconds) if float(wait_seconds).is_integer() else round(wait_seconds, 1)
        print(
            f"{provider} rate limited: {label} - waiting {shown_wait}s "
            f"(retry {attempts}/{max_retries})"
        )

        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if scan_control.should_stop():
                return response
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
