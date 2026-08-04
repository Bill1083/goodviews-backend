import json
import logging
import time
from typing import Any

import redis
import requests
from flask import current_app

logger = logging.getLogger(__name__)

_redis_client: redis.Redis | None = None
CACHE_TTL_SECONDS = 60 * 60 * 24  # 24 hours


def _get_redis() -> redis.Redis | None:
    global _redis_client
    if _redis_client is None:
        try:
            _redis_client = redis.from_url(
                current_app.config["REDIS_URL"], decode_responses=True
            )
            _redis_client.ping()
        except Exception:
            logger.warning("Redis unavailable — TMDB responses will not be cached.")
            _redis_client = None
    return _redis_client


def _cache_get(key: str) -> Any | None:
    r = _get_redis()
    if r is None:
        return None
    try:
        value = r.get(key)
        return json.loads(value) if value else None
    except Exception:
        return None


def _cache_set(key: str, value: Any) -> None:
    r = _get_redis()
    if r is None:
        return
    try:
        r.setex(key, CACHE_TTL_SECONDS, json.dumps(value))
    except Exception:
        pass


_MAX_ATTEMPTS = 6


def _tmdb_get(path: str, params: dict | None = None) -> dict:
    api_key = current_app.config["TMDB_API_KEY"]
    base_url = current_app.config["TMDB_BASE_URL"]
    merged_params = {"api_key": api_key, **(params or {})}
    last_exc: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            response = requests.get(
                f"{base_url}{path}", params=merged_params, timeout=10
            )
            response.raise_for_status()
            return response.json()
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
        except requests.HTTPError as exc:
            last_exc = exc
            # Retrying a 4xx (other than rate-limiting) would just fail the same way again.
            status = exc.response.status_code if exc.response is not None else None
            if status is not None and 400 <= status < 500 and status != 429:
                raise
        if attempt < _MAX_ATTEMPTS - 1:
            # TMDB intermittently resets the TLS connection from this host — measured at
            # roughly a 50% per-attempt failure rate, but each failure surfaces in
            # ~150-200ms, so several quick, lightly-backed-off retries clear it almost
            # every time without meaningfully adding to request latency.
            time.sleep(min(0.25 * (attempt + 1), 1.5))
    raise last_exc


def _sort_by_popularity(data: dict, key: str = "popularity") -> dict:
    """Sort the results list in a TMDB response by popularity descending."""
    if "results" in data:
        data["results"] = sorted(data["results"], key=lambda item: item.get(key, 0), reverse=True)
    return data


def search_movies(query: str, page: int = 1) -> dict:
    cache_key = f"tmdb:search:{query}:{page}"
    cached = _cache_get(cache_key)
    if cached:
        return _sort_by_popularity(cached)
    data = _tmdb_get("/search/movie", {"query": query, "page": page})
    _cache_set(cache_key, data)
    return _sort_by_popularity(data)


def get_movie_details(movie_id: int) -> dict:
    # Cache key includes "providers" so we don't serve a stale payload (missing
    # watch/providers) that was cached before that field was added to the request.
    cache_key = f"tmdb:movie:{movie_id}:with_credits_providers"
    cached = _cache_get(cache_key)
    if cached:
        return cached
    data = _tmdb_get(f"/movie/{movie_id}", {"append_to_response": "credits,watch/providers"})
    _cache_set(cache_key, data)
    return data


def get_movie_basic(movie_id: int) -> dict:
    """Lightweight movie fetch (no credits) used only for genre/rating enrichment.
    Reuses the full-details cache when already available to avoid a redundant call."""
    full_cache_key = f"tmdb:movie:{movie_id}:with_credits_providers"
    cached_full = _cache_get(full_cache_key)
    if cached_full:
        return cached_full
    cache_key = f"tmdb:movie:{movie_id}:basic"
    cached = _cache_get(cache_key)
    if cached:
        return cached
    data = _tmdb_get(f"/movie/{movie_id}")
    _cache_set(cache_key, data)
    return data


def get_trending_movies(page: int = 1) -> dict:
    """Most popular / most talked-about movies this week (TMDB trending)."""
    cache_key = f"tmdb:trending:week:{page}"
    cached = _cache_get(cache_key)
    if cached:
        return cached
    data = _tmdb_get("/trending/movie/week", {"page": page})
    _cache_set(cache_key, data)
    return data


def get_top_rated_movies(page: int = 1) -> dict:
    """All-time top-rated movies, used as a placeholder feed until personalized
    'For You' recommendations exist."""
    cache_key = f"tmdb:top_rated:{page}"
    cached = _cache_get(cache_key)
    if cached:
        return cached
    data = _tmdb_get("/movie/top_rated", {"page": page})
    _cache_set(cache_key, data)
    return data


def search_people(query: str, page: int = 1) -> dict:
    cache_key = f"tmdb:people:search:{query}:{page}"
    cached = _cache_get(cache_key)
    if cached:
        return _sort_by_popularity(cached)
    data = _tmdb_get("/search/person", {"query": query, "page": page, "include_adult": False})
    _cache_set(cache_key, data)
    return _sort_by_popularity(data)


def get_person_details(person_id: int) -> dict:
    cache_key = f"tmdb:person:{person_id}"
    cached = _cache_get(cache_key)
    if cached:
        return cached
    data = _tmdb_get(f"/person/{person_id}", {"append_to_response": "movie_credits"})
    _cache_set(cache_key, data)
    return data
