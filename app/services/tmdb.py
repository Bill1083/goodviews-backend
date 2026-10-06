import logging
import threading
import time
from collections.abc import Callable

import requests
from flask import current_app
from requests.adapters import HTTPAdapter

# Redis-backed response cache. The helpers live in app/services/cache.py
# (shared with the taste-stats caches); imported under the private names
# every call site below has always used.
from app.services.cache import CACHE_TTL_SECONDS, cache_get as _cache_get, cache_set as _cache_set

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 6

# (connect, read) for each attempt. A connect still pending after 3s is dead —
# retrying is quicker than waiting it out.
_TIMEOUT = (3.05, 8.0)

# Wall-clock budget for one call, across all of its retries. This used to be
# unbounded in practice: six attempts at a 10s timeout plus backoff is over a
# minute, long after the browser (15s axios timeout) had given up and shown
# "Something went wrong" — while the gunicorn worker stayed tied up the whole
# time, so a slow spell at TMDB made the rest of the API sluggish too. Kept
# under the client's timeout so the browser always gets a real answer.
_DEADLINE_SECONDS = 10.0

# Search results are cached for SEARCH_CACHE_TTL_SECONDS, and the last good
# copy is kept this much longer as a fallback for when TMDB can't be reached.
_STALE_TTL_SECONDS = 7 * 24 * 60 * 60

_local = threading.local()


def _session() -> requests.Session:
    """One keep-alive session per thread. Every call used to open a brand-new
    TCP + TLS connection, and new TLS handshakes are exactly what TMDB resets
    from this host (see the backoff note in _tmdb_get) — a warm pooled
    connection skips the handshake on most calls. Per thread rather than
    shared, because the For You and refresh jobs fan out over
    ThreadPoolExecutor workers."""
    session = getattr(_local, "session", None)
    if session is None:
        session = requests.Session()
        session.mount("https://", HTTPAdapter(pool_connections=2, pool_maxsize=4))
        _local.session = session
    return session


def _tmdb_get(path: str, params: dict | None = None) -> dict:
    api_key = current_app.config["TMDB_API_KEY"]
    base_url = current_app.config["TMDB_BASE_URL"]
    merged_params = {"api_key": api_key, **(params or {})}
    deadline = time.monotonic() + _DEADLINE_SECONDS
    last_exc: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        remaining = deadline - time.monotonic()
        if remaining <= 0.25:
            break
        try:
            response = _session().get(
                f"{base_url}{path}",
                params=merged_params,
                timeout=(min(_TIMEOUT[0], remaining), min(_TIMEOUT[1], remaining)),
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
            pause = min(0.25 * (attempt + 1), 1.5)
            if time.monotonic() + pause >= deadline:
                break
            time.sleep(pause)
    if last_exc is None:
        last_exc = requests.Timeout(f"TMDB {path}: no attempt fitted in {_DEADLINE_SECONDS}s")
    raise last_exc


def _search_key(kind: str, query: str, page: int) -> str:
    """TMDB search ignores case and extra spaces, so the cache does too —
    "Mirror Mask" and "mirror  mask" share one entry."""
    return f"tmdb:{kind}:{' '.join(query.lower().split())}:{page}"


def _cached_search(key: str, ttl: int, fetch: Callable[[], dict]) -> dict:
    """Fresh cache, else TMDB, else the last good copy. The stale fallback
    means a search someone has run before keeps working through a TMDB
    outage instead of turning into an error screen."""
    cached = _cache_get(key)
    if cached:
        return cached
    try:
        data = fetch()
    except Exception:
        stale = _cache_get(f"{key}:stale")
        if stale:
            logger.warning("TMDB unreachable; serving stale results for %s", key)
            return stale
        raise
    _cache_set(key, data, ttl=ttl)
    _cache_set(f"{key}:stale", data, ttl=_STALE_TTL_SECONDS)
    return data


def _sort_by_popularity(data: dict, key: str = "popularity") -> dict:
    """Sort the results list in a TMDB response by popularity descending."""
    if "results" in data:
        data["results"] = sorted(data["results"], key=lambda item: item.get(key, 0), reverse=True)
    return data


def search_movies(query: str, page: int = 1) -> dict:
    data = _cached_search(
        _search_key("search", query, page),
        current_app.config["SEARCH_CACHE_TTL_SECONDS"],
        lambda: _tmdb_get("/search/movie", {"query": query, "page": page}),
    )
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
    data = _cached_search(
        _search_key("people:search", query, page),
        CACHE_TTL_SECONDS,
        lambda: _tmdb_get("/search/person", {"query": query, "page": page, "include_adult": False}),
    )
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
