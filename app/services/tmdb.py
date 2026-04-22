import json
import logging
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


def _tmdb_get(path: str, params: dict | None = None) -> dict:
    api_key = current_app.config["TMDB_API_KEY"]
    base_url = current_app.config["TMDB_BASE_URL"]
    merged_params = {"api_key": api_key, **(params or {})}
    response = requests.get(
        f"{base_url}{path}", params=merged_params, timeout=10
    )
    response.raise_for_status()
    return response.json()


def search_movies(query: str, page: int = 1) -> dict:
    cache_key = f"tmdb:search:{query}:{page}"
    cached = _cache_get(cache_key)
    if cached:
        return cached
    data = _tmdb_get("/search/movie", {"query": query, "page": page})
    _cache_set(cache_key, data)
    return data


def get_movie_details(movie_id: int) -> dict:
    cache_key = f"tmdb:movie:{movie_id}"
    cached = _cache_get(cache_key)
    if cached:
        return cached
    data = _tmdb_get(f"/movie/{movie_id}")
    _cache_set(cache_key, data)
    return data
