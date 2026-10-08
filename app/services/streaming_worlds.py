"""One streaming service's own corner of Discover — tapping a service's tile
takes the user into a page scoped to just that service: Popular, For You, and
Different (a loose genre match), each a themed echo of the main Discover
sections but filtered to one provider.

Unlike streaming_picks.filter_by_availability (used by the main For You /
Movies of the Day filter), this never does a per-movie availability lookup —
TMDB's own /discover/movie already supports `with_watch_providers`, so the
provider scoping happens *at the source*, in one call per pool. That sidesteps
the whole class of bug the main filter needed several rounds to fix (see
streaming_picks.py's docstring and recommendations._backfill_items' deadline
param) — there's no N+1 here to bound in the first place.
"""
import logging
from concurrent.futures import ThreadPoolExecutor, wait

from flask import current_app

from app.services import streaming_picks, tmdb
from app.services.recommendations import _load_user_signals, _upsert_movie_stub
from app.services.supabase_client import get_supabase

logger = logging.getLogger(__name__)

REGION = streaming_picks.REGION
POOL_SIZE = 18

# Three discover calls run in parallel, each already bounded by tmdb's own
# per-call retry deadline (~10s) — this is the outer belt-and-braces bound on
# the whole batch, same philosophy as streaming_picks._AVAILABILITY_BUDGET_SECONDS.
_BUDGET_SECONDS = 8.0

_DISCOVER_BASE = {
    "include_adult": "false",
    "with_watch_monetization_types": "flatrate",
    "vote_count.gte": 50,
}


def _discover(provider_id: int, params: dict) -> list[dict]:
    try:
        data = tmdb.discover_movies({
            **_DISCOVER_BASE,
            "watch_region": REGION,
            "with_watch_providers": provider_id,
            **params,
        })
    except Exception:
        logger.exception("Streaming-world discover failed for provider %s params %s", provider_id, params)
        return []
    return [m for m in data.get("results", []) if m.get("id") and m.get("title") and m.get("poster_path")]


def _affinity_genres(signals) -> tuple[list[int], list[int]]:
    """(for_you genres, different genres) — top-ranked liked genres for the
    first, the next tier down (still liked, just not the user's favourites)
    for the second. Falls back to onboarding genres alone when there's no
    review history yet, so a brand-new account still gets *some* split
    between the two instead of two identical popularity lists."""
    ranked = sorted((g for g, w in signals.genre_affinity.items() if w > 0), key=lambda g: -signals.genre_affinity[g])
    if ranked:
        return ranked[:3], (ranked[3:6] or ranked[:3])
    onboarding = list(signals.onboarding_genre_ids)
    return onboarding[:3], onboarding[3:6]


def get_streaming_world(user_id: str, provider_id: int) -> dict:
    """{"popular": [...], "for_you": [...], "different": [...]}, each a list
    of slim movie dicts (same shape as a /discover response's "results"),
    deduped against each other so the three rows don't just repeat the same
    handful of blockbusters."""
    supabase = get_supabase()
    signals = _load_user_signals(user_id, supabase)
    for_you_genres, different_genres = _affinity_genres(signals)

    app = current_app._get_current_object()

    def _run(kind: str) -> tuple[str, list[dict]]:
        with app.app_context():
            if kind == "popular":
                return kind, _discover(provider_id, {"sort_by": "popularity.desc"})
            genres = for_you_genres if kind == "for_you" else different_genres
            if not genres:
                return kind, []
            return kind, _discover(provider_id, {"with_genres": "|".join(str(g) for g in genres), "sort_by": "vote_average.desc"})

    pools: dict[str, list[dict]] = {"popular": [], "for_you": [], "different": []}
    executor = ThreadPoolExecutor(max_workers=3)
    try:
        futures = {executor.submit(_run, kind): kind for kind in pools}
        done, not_done = wait(futures, timeout=_BUDGET_SECONDS)
        for f in done:
            try:
                kind, results = f.result()
                pools[kind] = results
            except Exception:
                logger.exception("Streaming-world pool fetch raised for provider %s", provider_id)
        if not_done:
            logger.warning(
                "Streaming-world fetch timed out for provider %s (%d/%d pools)", provider_id, len(not_done), len(pools)
            )
    finally:
        # Same reasoning as streaming_picks.filter_by_availability: don't
        # block the response on a straggler past the budget.
        executor.shutdown(wait=False)

    # A cold-start account (no genre affinity yet) or a service with thin
    # catalog overlap on the chosen genres can leave for_you/different empty
    # — fall back to a well-rated general pool rather than a dead section.
    if not pools["for_you"]:
        pools["for_you"] = _discover(provider_id, {"sort_by": "vote_average.desc"})
    if not pools["different"]:
        pools["different"] = _discover(provider_id, {"sort_by": "popularity.desc", "page": 2})

    seen: set[int] = set()
    out: dict[str, list[dict]] = {}
    for kind in ("popular", "for_you", "different"):
        unique: list[dict] = []
        for m in pools[kind]:
            if m["id"] in seen:
                continue
            seen.add(m["id"])
            unique.append(m)
            if len(unique) >= POOL_SIZE:
                break
        out[kind] = unique

    stubs = [_upsert_movie_stub(m) for films in out.values() for m in films]
    if stubs:
        try:
            supabase.table("movies").upsert(stubs, on_conflict="id", ignore_duplicates=True).execute()
        except Exception:
            logger.exception("Failed to upsert streaming-world movie stubs")

    def _slim(m: dict) -> dict:
        return {
            "id": m["id"],
            "title": m.get("title"),
            "poster_path": m.get("poster_path"),
            "backdrop_path": m.get("backdrop_path"),
            "release_date": m.get("release_date"),
            "vote_average": m.get("vote_average") or 0,
            "genre_ids": m.get("genre_ids") or [],
        }

    return {kind: [_slim(m) for m in films] for kind, films in out.items()}
