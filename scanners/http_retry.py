"""Shared HTTP retry helpers for source scanners.

Transient GET transport failures and throttling (HTTP 429) are retried here. Other HTTP statuses are
returned to the caller so each source can preserve its existing handling.
"""

from __future__ import annotations

import email.utils
import json
import random
import math
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
_PACE_BLOCKED_UNTIL = {}
_PACE_MIN_FLOOR = {}
_CACHE_LOCK = threading.RLock()
_SCAN_CACHE = {}
_CACHE_KEY_LOCKS = {}

def reset_retry_stats():
    with _RETRY_LOCK:
        _RETRY_STATS.clear()
    with _PACE_LOCK:
        _PACE_LAST.clear()
        _PACE_STATS.clear()
        _PACE_BLOCKED_UNTIL.clear()
        _PACE_MIN_FLOOR.clear()
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
    if not key:
        return
    min_interval = max(0.0, float(min_interval or 0.0))
    waited = 0.0
    while True:
        with _PACE_LOCK:
            now = time.monotonic()
            last = _PACE_LAST.get(key)
            effective_interval = max(min_interval, float(_PACE_MIN_FLOOR.get(key, 0.0) or 0.0))
            interval_remaining = 0.0 if last is None else effective_interval - (now - last)
            cooldown_remaining = float(_PACE_BLOCKED_UNTIL.get(key, 0.0) or 0.0) - now
            remaining = max(0.0, interval_remaining, cooldown_remaining)
            if remaining <= 0:
                _PACE_LAST[key] = now
                item = _PACE_STATS.setdefault(key, {"requests": 0, "wait_seconds": 0.0, "cache_hits": 0})
                item["requests"] += 1
                item["wait_seconds"] += waited
                return
        if scan_control.should_stop():
            return
        step = min(0.10, remaining)
        time.sleep(step)
        waited += step


def _raise_pace_floor(key, seconds):
    """Raise a host's scan-local minimum spacing after a real throttle."""
    if not key:
        return
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return
    if seconds <= 0:
        return
    with _PACE_LOCK:
        if seconds > float(_PACE_MIN_FLOOR.get(key, 0.0) or 0.0):
            _PACE_MIN_FLOOR[key] = seconds


def _register_pace_cooldown(key, seconds):
    """Share a provider-declared 429 cooldown with sibling scan requests."""
    if not key:
        return
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return
    if seconds <= 0:
        return
    with _PACE_LOCK:
        until = time.monotonic() + seconds
        if until > float(_PACE_BLOCKED_UNTIL.get(key, 0.0) or 0.0):
            _PACE_BLOCKED_UNTIL[key] = until

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

def get_cached_json_with_backoff(
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
    """Return ``(status_code, payload, cache_hit)`` for scan-local JSON.

    The cached value is the raw response text rather than a mutable dict/list,
    so concurrent CivitAI/CivitAI Red workers always receive their own parsed
    payload. Per-key locking also coalesces identical in-flight detail requests.
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
            try:
                return 200, json.loads(cached), True
            except Exception:
                # A corrupt/obsolete cache entry should never poison a scan.
                with _CACHE_LOCK:
                    _SCAN_CACHE.pop(cache_key, None)

        response = get_with_backoff(
            session, url, provider=provider, label=label, max_retries=max_retries,
            backoff=backoff, pace_key=pace_key, min_interval=min_interval, **kwargs
        )
        text = response.text or ""
        payload = {}
        if response.status_code == 200:
            try:
                payload = response.json()
            except Exception:
                try:
                    payload = json.loads(text)
                except Exception:
                    payload = {}
            if isinstance(payload, (dict, list)):
                with _CACHE_LOCK:
                    _SCAN_CACHE[cache_key] = text
        return response.status_code, payload, False


def _record_retry(provider, recovered=False, failed=False, reason=""):
    with _RETRY_LOCK:
        item = _RETRY_STATS.setdefault(provider, {"retries": 0, "recovered": 0, "failed": 0})
        if not recovered and not failed:
            item["retries"] += 1
        if recovered:
            item["recovered"] += 1
        if failed:
            item["failed"] += 1
            item["last_error"] = reason or "HTTP rate limit retries exhausted"



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
    cancellable=None,
    **kwargs,
):
    """GET with bounded, cancellable transport and HTTP 429 retries.

    Returns the final ``requests.Response``. If all retries are exhausted the
    final 429 response is returned so the source scanner can decide how to
    report or abort that individual search.
    """
    attempts = 0
    cancellable = scan_control.cancellation_enabled() if cancellable is None else bool(cancellable)

    def check_cancelled():
        if cancellable:
            scan_control.check_cancelled()

    def wait(seconds):
        if cancellable:
            scan_control.wait_or_cancel(seconds)
        else:
            time.sleep(seconds)
    max_retries = max(0, min(int(max_retries), 5))
    kwargs.setdefault("timeout", (10, 30))

    while True:
        check_cancelled()
        _pace_request(pace_key, min_interval)
        check_cancelled()
        try:
            response = session.get(url, **kwargs)
        except (requests.exceptions.SSLError, requests.exceptions.InvalidURL):
            raise
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            check_cancelled()
            if attempts >= max_retries:
                _record_retry(provider, failed=True, reason=failure_message(exc))
                raise
            wait_seconds = min(15.0, 2 ** attempts + random.uniform(0, 0.5))
            attempts += 1
            _record_retry(provider)
            # Do not log request URLs, headers, or exception strings containing tokens.
            print(f"{provider}: {failure_message(exc)}; retry {attempts}/{max_retries} in {wait_seconds:.1f}s")
            wait(wait_seconds)
            continue
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
        if not math.isfinite(wait_seconds) or wait_seconds > 120:
            # Return the rate-limit response without retrying sooner than requested.
            _record_retry(provider, failed=True)
            return response
        # A Retry-After applies to the destination, not only this thread. Share
        # that cooldown with sibling CivitAI/Red requests so concurrent detail
        # hydration cannot immediately walk into the same 429 window.
        _register_pace_cooldown(pace_key, wait_seconds)
        # The public CivitAI detail/version endpoints have been stable at our
        # faster 1.0s test pace, but if the provider actually throttles us,
        # immediately return to the proven 1.25s spacing for the rest of this
        # scan instead of repeatedly probing the faster rate.
        if pace_key == "CivitAI.com" and float(min_interval or 0.0) < 1.25:
            _raise_pace_floor(pace_key, 1.25)
        attempts += 1
        _record_retry(provider)

        # Keep logs useful but compact. Avoid printing response bodies because
        # Cloudflare/provider pages can be very large.
        shown_wait = int(wait_seconds) if float(wait_seconds).is_integer() else round(wait_seconds, 1)
        print(
            f"{provider} rate limited: {label} - waiting {shown_wait}s "
            f"(retry {attempts}/{max_retries})"
        )

        response.close()
        wait(wait_seconds)


def failure_message(exc):
    """Safe UI category; full chained exceptions remain in diagnostic tracebacks."""
    if isinstance(exc, scan_control.ScanCancelled):
        return "Scan stopped by user"
    text = str(exc).lower()
    if "browserstartup" in type(exc).__name__.lower():
        return str(exc)
    if any(word in text for word in ("nameresolution", "getaddrinfo", "name resolution", "nodename nor servname")):
        return "DNS resolution failed; check connectivity and VPN DNS"
    if any(word in text for word in ("remotedisconnected", "remote end closed", "connection aborted")):
        return "Remote server disconnected; retry after checking connectivity"
    if isinstance(exc, requests.exceptions.SSLError):
        return "TLS certificate or secure connection failure"
    if isinstance(exc, requests.exceptions.Timeout):
        return "Network request timed out"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "Network connection failed; check connectivity"
    if "429" in text or "rate limit" in text:
        return "HTTP rate limit; retry later"
    return f"Source failed ({type(exc).__name__}); see diagnostic log"
