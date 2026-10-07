"""Backs two things: the curated streaming-provider picker in Settings, and
the "Your Streaming Services" Discover carousel — popular/well-rated/genre-
affinity films, filtered to only what's actually available (flatrate, i.e.
subscription-included) on the providers a user says they have. Deliberately
sourced from TMDB's evergreen catalog (popularity/vote_average/genre), not
now_playing/upcoming — this carousel is "what can I watch tonight", not new
releases (that's Movies of the Day).
"""
import logging
import random

from flask import current_app

from app.services import tmdb
from app.services.cache import cache_get, cache_set

logger = logging.getLogger(__name__)

# Matches the region MovieDetailModal's own WatchProvidersModal already uses
# (see client/src/components/MovieDetailModal.tsx) — one region for now,
# supporting more later is a param, not a redesign.
REGION = "AU"

# Case-insensitive substring match against TMDB's provider_name, rather than
# hardcoding provider_ids — those vary by region/time and aren't worth
# pinning from memory; names are stable and this tolerates TMDB's exact
# wording changing (e.g. "Amazon Prime Video" still matches "prime video").
# Order here is the selection grid's display order.
CURATED_PROVIDER_KEYWORDS = [
    "netflix",
    "prime video",
    "disney plus",
    "stan",
    "binge",
    "paramount plus",
    "apple tv",
    "foxtel now",
    "hayu",
    "britbox",
    "crunchyroll",
    "sbs on demand",
]

_MOVIE_FIELDS = ("id", "title", "poster_path", "backdrop_path", "release_date", "overview", "vote_average", "genre_ids")

# How many TMDB discover pages deep a "popular" pool call picks from —
# re-rolled each time the pool's cache entry expires, so the carousel
# doesn't show the literal same top-20-popular set forever.
_RANDOM_POOL_PAGES = 5

_POOL_SIZE = 24


def _rank(provider_name: str) -> int:
    lowered = provider_name.lower()
    for i, keyword in enumerate(CURATED_PROVIDER_KEYWORDS):
        if keyword in lowered:
            return i
    return len(CURATED_PROVIDER_KEYWORDS)


def list_streaming_providers() -> list[dict]:
    """The curated subset of TMDB's AU provider catalog offered in Settings.
    provider_id/provider_name/logo_path are always TMDB's own — we only
    decide which ones are worth offering."""
    data = tmdb.get_watch_providers_list(REGION)
    seen_ids: set[int] = set()
    curated: list[dict] = []
    for p in data.get("results", []):
        name = p.get("provider_name", "")
        pid = p.get("provider_id")
        if pid is None or pid in seen_ids or _rank(name) == len(CURATED_PROVIDER_KEYWORDS):
            continue
        seen_ids.add(pid)
        curated.append({"provider_id": pid, "provider_name": name, "logo_path": p.get("logo_path")})
    curated.sort(key=lambda p: _rank(p["provider_name"]))
    return curated


def _slim(movie: dict) -> dict:
    return {field: movie.get(field) for field in _MOVIE_FIELDS}


def _discover_pool(provider_ids: list[int], extra_params: dict, cache_suffix: str) -> list[dict]:
    """One TMDB /discover/movie page, filtered to the given providers —
    cached and shared across every user with this exact provider selection
    (keyed by the provider set, not by user), since the underlying catalog
    is the same for everyone with the same services."""
    provider_key = ",".join(str(p) for p in sorted(provider_ids))
    cache_key = f"streaming_picks:{provider_key}:{cache_suffix}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    params = {
        "watch_region": REGION,
        "with_watch_providers": "|".join(str(p) for p in provider_ids),
        "with_watch_monetization_types": "flatrate",
        "include_adult": "false",
        **extra_params,
    }
    try:
        data = tmdb.discover_movies(params)
    except Exception:
        logger.exception("Streaming-picks discover call failed (%s)", cache_suffix)
        return []

    results = [_slim(m) for m in data.get("results", [])]
    ttl = current_app.config["STREAMING_PICKS_TTL_HOURS"] * 3600
    cache_set(cache_key, results, ttl=ttl)
    return results


def get_streaming_picks(provider_ids: list[int], genre_ids: list[int], exclude_movie_ids: set[int]) -> list[dict]:
    """Popular + well-rated + (if the user has onboarding genre picks)
    genre-affinity pools, filtered to the given providers, merged and
    interleaved so no single pool dominates the front of the carousel.
    exclude_movie_ids is the "hide films I've already seen" toggle's effect
    — the caller decides what that set is (here: the user's reviewed
    movie_ids) so this function stays about pool-building, not DB access."""
    if not provider_ids:
        return []

    popular_page = random.randint(1, _RANDOM_POOL_PAGES)
    popular = _discover_pool(
        provider_ids, {"sort_by": "popularity.desc", "page": popular_page}, f"popular:{popular_page}",
    )
    top_rated = _discover_pool(
        provider_ids, {"sort_by": "vote_average.desc", "vote_count.gte": 200}, "top_rated",
    )
    for_you: list[dict] = []
    if genre_ids:
        genre_key = ",".join(str(g) for g in sorted(genre_ids))
        for_you = _discover_pool(
            provider_ids, {"sort_by": "popularity.desc", "with_genres": genre_key}, f"for_you:{genre_key}",
        )

    pools = [p for p in (for_you, popular, top_rated) if p]
    merged: list[dict] = []
    seen_ids: set[int] = set(exclude_movie_ids)
    idx = 0
    while len(merged) < _POOL_SIZE and any(idx < len(p) for p in pools):
        for p in pools:
            if idx >= len(p):
                continue
            m = p[idx]
            if m["id"] in seen_ids:
                continue
            seen_ids.add(m["id"])
            merged.append(m)
        idx += 1

    return merged[:_POOL_SIZE]
