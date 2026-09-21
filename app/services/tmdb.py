import logging
import time

import requests
from flask import current_app

# Redis-backed response cache. The helpers live in app/services/cache.py
# (shared with the taste-stats caches); imported under the private names
# every call site below has always used.
from app.services.cache import cache_get as _cache_get, cache_set as _cache_set

logger = logging.getLogger(__name__)

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
    """All-time top-rated movies — used both as its own Discover tab and as
    the cold-start/backfill source for 'For You' (see app.services.recommendations)."""
    cache_key = f"tmdb:top_rated:{page}"
    cached = _cache_get(cache_key)
    if cached:
        return cached
    data = _tmdb_get("/movie/top_rated", {"page": page})
    _cache_set(cache_key, data)
    return data


def get_movie_recommendations(movie_id: int, page: int = 1) -> dict:
    """TMDB's own "people who liked this also liked" list for one movie —
    the primary content-based candidate source for 'For You', seeded from a
    user's own highly-rated movies. Not Redis-cached: seed-specific, low
    repeat-hit-rate, same reasoning as fetch_movie_segments."""
    return _tmdb_get(f"/movie/{movie_id}/recommendations", {"page": page})


def get_similar_movies(movie_id: int, page: int = 1) -> dict:
    """Fallback candidate source when a seed's /recommendations list is thin
    — TMDB's genre/keyword-similarity list rather than its collaborative one."""
    return _tmdb_get(f"/movie/{movie_id}/similar", {"page": page})


def discover_movies(params: dict) -> dict:
    """Thin passthrough to TMDB's /discover/movie — used to generate
    candidates from a favourite actor/director (with_cast/with_crew) rather
    than from a specific seed movie."""
    return _tmdb_get("/discover/movie", params)


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


def get_popular_people(page: int = 1) -> dict:
    """Generically popular actors/directors — used as filler in the
    onboarding favourites grid alongside genre/movie-seeded suggestions."""
    cache_key = f"tmdb:people:popular:{page}"
    cached = _cache_get(cache_key)
    if cached:
        return cached
    data = _tmdb_get("/person/popular", {"page": page})
    _cache_set(cache_key, data)
    return data
