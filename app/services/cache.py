"""Process-wide Redis helpers, shared by the TMDB proxy cache and the taste
stats / Wrapped caches. Everything here degrades to a no-op when Redis is
unavailable (local dev without a Redis, or a transient outage) — callers
never guard for it; a missing cache just means recomputing.

These started life as private helpers inside app/services/tmdb.py; the taste
dashboard needed the same connection for user-scoped keys, so they moved."""
import json
import logging
import time
from typing import Any

import redis
from flask import current_app

logger = logging.getLogger(__name__)

_redis_client: redis.Redis | None = None
_retry_after: float = 0.0
_RETRY_INTERVAL_SECONDS = 30.0

CACHE_TTL_SECONDS = 60 * 60 * 24  # 24 hours
STATS_VERSION_TTL_SECONDS = 30 * 24 * 60 * 60  # comfortably longer than any cached payload's TTL


def get_redis() -> redis.Redis | None:
    """Lazy singleton. A failed connection is remembered for
    _RETRY_INTERVAL_SECONDS so an unreachable Redis costs one short timeout
    every 30s per process — not one per cache lookup, and a dashboard
    request makes several."""
    global _redis_client, _retry_after
    if _redis_client is not None:
        return _redis_client
    if time.monotonic() < _retry_after:
        return None
    try:
        client = redis.from_url(
            current_app.config["REDIS_URL"],
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        client.ping()
        _redis_client = client
    except Exception:
        logger.warning("Redis unavailable — caching disabled (will retry in %ss).", int(_RETRY_INTERVAL_SECONDS))
        _redis_client = None
        _retry_after = time.monotonic() + _RETRY_INTERVAL_SECONDS
    return _redis_client


def cache_get(key: str) -> Any | None:
    r = get_redis()
    if r is None:
        return None
    try:
        value = r.get(key)
        return json.loads(value) if value else None
    except Exception:
        return None


def cache_set(key: str, value: Any, ttl: int | None = None) -> None:
    r = get_redis()
    if r is None:
        return
    try:
        r.setex(key, ttl or CACHE_TTL_SECONDS, json.dumps(value))
    except Exception:
        pass


def cache_delete(*keys: str) -> None:
    r = get_redis()
    if r is None or not keys:
        return
    try:
        r.delete(*keys)
    except Exception:
        pass


def cache_incr(key: str, ttl: int | None = None) -> int | None:
    """INCR plus a (re)applied EXPIRE. Returns the new value, or None when
    Redis is down."""
    r = get_redis()
    if r is None:
        return None
    try:
        value = r.incr(key)
        if ttl:
            r.expire(key, ttl)
        return int(value)
    except Exception:
        return None


def stats_version(user_id: str) -> int:
    """Current cache generation for one user's stats keys — 0 when unset, or
    when Redis is down (in which case nothing is cached anyway)."""
    r = get_redis()
    if r is None:
        return 0
    try:
        return int(r.get(f"stats:ver:{user_id}") or 0)
    except Exception:
        return 0


def invalidate_user_stats(user_id: str) -> None:
    """Bumps the user's stats generation so every cached dashboard / Wrapped
    payload for them is orphaned (the orphans simply expire by TTL).

    One O(1) INCR rather than enumerating keys: a single review edit can
    change the all-time dashboard, the current year's Wrapped AND a past
    year's Wrapped (deleting an old review), across every timezone variant
    that was ever requested — a wildcard delete would need SCAN. Non-fatal
    by construction; a write must never fail because the cache did."""
    try:
        cache_incr(f"stats:ver:{user_id}", ttl=STATS_VERSION_TTL_SECONDS)
    except Exception:
        logger.exception("Failed to invalidate stats cache for %s", user_id)
