"""Backs two things: the curated streaming-provider picker in Settings, and
the "Only show what I can stream" filter applied to For You and Movies of
the Day — NOT to Most Popular This Week, which stays unfiltered on purpose
(it's current/trending titles, including ones still only in cinemas, so
"is this on your streaming services" doesn't apply to it the way it does to
the personalized/evergreen feeds).
"""
import logging
from concurrent.futures import ThreadPoolExecutor, wait

from flask import current_app

from app.services import movie_cache, tmdb

logger = logging.getLogger(__name__)

# How long filter_by_availability is willing to wait for ALL of a batch's
# per-movie provider lookups combined, regardless of how many movies or how
# slow/flaky TMDB is being — past this, anything still unresolved is
# treated as available (same fail-open philosophy as a single lookup's own
# exception handling, just for "too slow" instead of "errored"). Without an
# outer bound here, a batch of movies whose "providers" segment has never
# been fetched before (true for most of a recommendation feed — that
# segment is normally only warmed by someone actually opening a movie's
# detail page) hitting TMDB's own documented connection-reset flakiness
# (up to 6 retries, 10s deadline *per movie* — see tmdb._DEADLINE_SECONDS)
# could collectively take far longer than the client's own request
# timeout, which is exactly what "nothing ever loaded" was.
_AVAILABILITY_BUDGET_SECONDS = 2.5

# Matches the region MovieDetailModal's own WatchProvidersModal already uses
# (see client/src/components/MovieDetailModal.tsx) — one region for now,
# supporting more later is a param, not a redesign.
REGION = "AU"

# Exact (case-insensitive) TMDB provider_name matches for the AU catalog —
# verified against a live call, not guessed from memory. Deliberately *not*
# a substring match: that pulled in bundle/add-on listings TMDB lists as
# separate providers ("Britbox Amazon Channel", "AMC Plus Apple TV
# channel") and alternate ad/kids/basic tiers ("Netflix Standard with
# Ads") just because they contain a core name — exact names avoid that
# while still being stable (a core service's TMDB name essentially never
# changes). Order here is the selection grid's display order.
#
# Hayu has no standalone AU entry on TMDB as of this writing, only "Hayu
# Amazon Channel" (a bundle listing) — left out rather than offered under a
# confusing name; add it back if TMDB ever lists it standalone.
CURATED_PROVIDER_NAMES = [
    "netflix",
    "amazon prime video",
    "disney plus",
    "stan",
    "binge",
    "paramount plus",
    "apple tv",
    "foxtel now",
    "britbox",
    "crunchyroll",
    "sbs on demand",
]


def _rank(provider_name: str) -> int:
    lowered = provider_name.strip().lower()
    try:
        return CURATED_PROVIDER_NAMES.index(lowered)
    except ValueError:
        return len(CURATED_PROVIDER_NAMES)


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
        if pid is None or pid in seen_ids or _rank(name) == len(CURATED_PROVIDER_NAMES):
            continue
        seen_ids.add(pid)
        curated.append({"provider_id": pid, "provider_name": name, "logo_path": p.get("logo_path")})
    curated.sort(key=lambda p: _rank(p["provider_name"]))
    return curated


def _available_on(movie_id: int, provider_ids: set[int], app) -> bool:
    """Flatrate (subscription-included, not rent/buy) availability for one
    movie against the given provider ids, via the same per-movie TMDB
    watch-providers cache MovieDetailModal's "where to watch" already uses
    (movie_cache's "providers" segment — a Postgres read on any cache hit,
    which is the overwhelming majority of calls once a movie's been looked
    at once). Fails open (keeps the movie) on any lookup error — a provider
    lookup hiccup should never be why a film silently vanishes from a feed."""
    with app.app_context():
        try:
            data = movie_cache.get_movie(movie_id, segments=("providers",))
        except Exception:
            logger.exception("Streaming-filter provider lookup failed for movie %s", movie_id)
            return True
    region_data = (data.get("watch/providers") or {}).get("results", {}).get(REGION) or {}
    flatrate = region_data.get("flatrate") or []
    return any(p.get("provider_id") in provider_ids for p in flatrate)


def filter_by_availability(movies: list[dict], provider_ids: list[int]) -> list[dict]:
    """Keeps only movies available (flatrate) on at least one of provider_ids,
    preserving the input order. A no-op if provider_ids is empty — filtering
    "my streaming services" down to zero services isn't "show nothing", it's
    "there's nothing to filter by", same as the toggle being off.

    Looks up every movie in parallel (same reasoning/pattern as reviews.py's
    _enrich_movies: movie_cache.get_movie needs current_app, which a
    ThreadPoolExecutor worker doesn't inherit on its own) — a feed is at
    most ~20 films, and almost every lookup is a cached Postgres read, but a
    fully cold cache doing them one at a time would otherwise add up.
    Bounded to _AVAILABILITY_BUDGET_SECONDS total — see that constant."""
    if not provider_ids or not movies:
        return movies

    ids = set(provider_ids)
    app = current_app._get_current_object()
    executor = ThreadPoolExecutor(max_workers=min(len(movies), 10))
    try:
        futures = {executor.submit(_available_on, m["id"], ids, app): m["id"] for m in movies}
        done, not_done = wait(futures, timeout=_AVAILABILITY_BUDGET_SECONDS)

        keep_ids: set[int] = set()
        for f in done:
            try:
                if f.result():
                    keep_ids.add(futures[f])
            except Exception:
                keep_ids.add(futures[f])  # fail open — see _available_on
        if not_done:
            logger.warning("Streaming-filter availability check timed out for %d/%d movies", len(not_done), len(movies))
            keep_ids |= {futures[f] for f in not_done}
    finally:
        # Don't block returning on stragglers past the budget above — let
        # them finish warming the cache in the background unsupervised,
        # harmless since they're just reads. (shutdown(wait=True), the
        # default via `with`, would defeat the timeout entirely by blocking
        # here until every thread finishes regardless.)
        executor.shutdown(wait=False)
    return [m for m in movies if m["id"] in keep_ids]
