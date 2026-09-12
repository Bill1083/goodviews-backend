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


def _cache_set(key: str, value: Any, ttl: int | None = None) -> None:
    r = _get_redis()
    if r is None:
        return
    try:
        r.setex(key, ttl or CACHE_TTL_SECONDS, json.dumps(value))
    except Exception:
        pass


def _tmdb_get(path: str, params: dict | None = None) -> dict:
    api_key = current_app.config["TMDB_API_KEY"]
    base_url = current_app.config["TMDB_BASE_URL"]
    merged_params = {"api_key": api_key, **(params or {})}
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            response = requests.get(
                f"{base_url}{path}", params=merged_params, timeout=10
            )
            response.raise_for_status()
            return response.json()
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
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
    _cache_set(cache_key, data, ttl=current_app.config["SEARCH_CACHE_TTL_SECONDS"])
    return _sort_by_popularity(data)


SEGMENT_TOKENS = {"core": "credits", "media": "videos", "providers": "watch/providers"}


def fetch_movie_segments(movie_id: int, segments: set[str]) -> dict:
    """Fetch exactly the requested segments for a movie in ONE TMDB call via
    append_to_response. Always hits TMDB live — no Redis caching here; freshness
    for movie details is governed by Postgres timestamp columns, one layer up
    in app.services.movie_cache."""
    tokens = [SEGMENT_TOKENS[s] for s in segments if s in SEGMENT_TOKENS]
    params = {"append_to_response": ",".join(tokens)} if tokens else None
    return _tmdb_get(f"/movie/{movie_id}", params)


def get_movie_images(movie_id: int) -> dict:
    """Poster/backdrop/logo art for a movie — kept as its own call (not appended to
    the details fetch) so the AvatarPicker's poster grid doesn't balloon the payload
    of the main movie-details endpoint that every other movie view also relies on.
    Cached in Redis (not Postgres — this is an unbounded gallery array, not a single
    scalar/small-jsonb field) using the same freshness window as the media segment."""
    cache_key = f"tmdb:movie:{movie_id}:images"
    cached = _cache_get(cache_key)
    if cached:
        return cached
    data = _tmdb_get(f"/movie/{movie_id}/images")
    ttl = current_app.config["MOVIE_MEDIA_TTL_DAYS"] * 86400
    _cache_set(cache_key, data, ttl=ttl)
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
