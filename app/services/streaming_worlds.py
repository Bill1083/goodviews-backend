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

For You and Different used to be a single deterministic discover call each
(fixed sort_by, always page 1) keyed only on a bucketed top-3/next-3 slice of
the user's genre_affinity — a ~19-genre space. Any two users who landed on the
same ordered genre tuple (easy, especially via the onboarding-genre fallback
for a thin review history) got a byte-identical query and byte-identical
results. Both sections now (a) draw from a per-(user, day, provider, section)
randomized page/sort, same seeding technique as daily_picks.py's Movies of the
Day, and (b) re-rank whatever TMDB returns by a blend of genre match, overview-
text similarity to the user's own highly-rated movies (see
recommendations.build_taste_profile_terms — no typed-in user input, just their
existing reviews), and popularity. Popular is deliberately left untouched: it's
a shared "what's trending here" row, not a personal one, so staying identical
across users is correct, not a bug.
"""
import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import date

from flask import current_app

from app.services import cache, streaming_picks, tmdb
from app.services.recommendations import (
    _load_user_signals,
    _upsert_movie_stub,
    build_taste_profile_terms,
    overview_similarity,
)
from app.services.seeded_rng import seeded_rng, weighted_sample
from app.services.supabase_client import get_supabase

logger = logging.getLogger(__name__)

REGION = streaming_picks.REGION

# Every section is topped up to the same length regardless of how deep its
# own genre-scoped catalog goes — two pages of 10 on the client (see
# StreamingWorldPage's pagination), never a visibly shorter "Different" row.
TARGET_POOL_SIZE = 20
PAGE_SIZE = 10

# Three discover calls run in parallel, each already bounded by tmdb's own
# per-call retry deadline (~10s) — this is the outer belt-and-braces bound on
# the whole batch, same philosophy as streaming_picks._AVAILABILITY_BUDGET_SECONDS.
_BUDGET_SECONDS = 8.0

# Padding a short pool (most often "Different", whose genre scoping can
# easily come up thin for a smaller catalog) is a handful of *sequential*
# extra discover calls, done after the parallel phase above — bounded the
# same way recommendations._backfill_items bounds its own catch-up loop:
# checked between pages, not able to abort one already in flight.
_PAD_DEADLINE_SECONDS = 3.0
_PAD_MAX_PAGE = 4

_DISCOVER_BASE = {
    "include_adult": "false",
    "with_watch_monetization_types": "flatrate",
    "vote_count.gte": 50,
}

# How many of the first TMDB result pages a For You/Different discover call
# may land on, and which sort it may use — chosen per (user, day, provider,
# section), not fixed, so two users sharing a genre tuple no longer share a
# query. Popular never gets this: it's meant to be the same for everyone.
_DISCOVER_PAGE_POOL = 3
_DISCOVER_SORTS = ("vote_average.desc", "popularity.desc")

# Blend weights for re-ranking a section's (already provider/genre-filtered)
# pool — genre-tuple overlap, overview-text similarity to the user's own
# highly-rated movies, and raw popularity. Re-ranks what TMDB already
# returned rather than replacing the discover call outright: at this app's
# scale (~20 candidates per section) that keeps the watch-region/provider
# correctness TMDB's own filter already gives for free. Named constants, not
# inline literals, so they're a one-line tuning knob once there's real usage
# data — no ground truth behind these starting values yet.
_SCORE_WEIGHTS = {"genre": 0.4, "overview": 0.4, "popularity": 0.2}
# No taste profile yet (cold start, or no cached overview text) — overview is
# dropped entirely, not just zeroed, so a missing signal doesn't silently
# subtract ranking weight; renormalized across the remaining two terms.
_SCORE_WEIGHTS_NO_PROFILE = {"genre": 0.6, "popularity": 0.4}


def _discover(
    provider_id: int,
    params: dict,
    exclude_ids: set[int] | None = None,
    rng: random.Random | None = None,
) -> list[dict]:
    call_params = dict(params)
    if rng is not None and "page" not in call_params:
        call_params["page"] = rng.randint(1, _DISCOVER_PAGE_POOL)
    try:
        data = tmdb.discover_movies({
            **_DISCOVER_BASE,
            "watch_region": REGION,
            "with_watch_providers": provider_id,
            **call_params,
        })
    except Exception:
        logger.exception("Streaming-world discover failed for provider %s params %s", provider_id, call_params)
        return []
    results = [m for m in data.get("results", []) if m.get("id") and m.get("title") and m.get("poster_path")]
    if exclude_ids:
        results = [m for m in results if m["id"] not in exclude_ids]
    if rng is not None:
        # If the randomly-chosen page came back short (e.g. page 3 of a
        # thin catalog), don't retry at page 1 — a short-but-real page is
        # still a legitimate, differently-ordered result; the padding
        # filler later tops up anything that ends up short overall.
        rng.shuffle(results)
    return results


def _genre_match_score(movie: dict, target_genres: list[int]) -> float:
    if not target_genres:
        return 0.0
    overlap = len(set(movie.get("genre_ids") or []) & set(target_genres))
    return overlap / len(target_genres)


def _score_and_order(
    results: list[dict],
    target_genres: list[int],
    profile_terms,
    rng: random.Random,
) -> list[dict]:
    """Re-ranks (not re-fetches) a section's discover results by a blend of
    genre match, overview-text similarity to the user's taste profile, and
    popularity — then draws a full weighted-random order from that blend
    (Efraimidis-Spirakis, see seeded_rng.weighted_sample) rather than a hard
    sort, so even two users who land on the identical randomized page still
    diverge in what shows first."""
    if not results:
        return results
    weights = _SCORE_WEIGHTS if profile_terms else _SCORE_WEIGHTS_NO_PROFILE
    scored: list[tuple[float, dict]] = []
    for m in results:
        score = (
            weights["genre"] * _genre_match_score(m, target_genres)
            + weights["popularity"] * ((m.get("vote_average") or 0) / 10)
        )
        if profile_terms:
            score += weights["overview"] * overview_similarity(profile_terms, m.get("overview"))
        scored.append((max(score, 1e-6), m))
    return weighted_sample(scored, len(scored), rng)


def _affinity_genres(signals) -> tuple[list[int], list[int]]:
    """(for_you genres, different genres) — top-ranked liked genres for the
    first, the next tier down (still liked, just not the user's favourites)
    for the second. different_genres is deliberately [] rather than falling
    back to for_you_genres when there's no distinct next tier: get_streaming_world's
    own dedup already strips anything "different" shares with "popular"/
    "for_you", so reusing the same genres here would just hand it a pool
    that's entirely deduped away — [] correctly routes it to that function's
    general popularity fallback instead, a real (if less personalized) list."""
    ranked = sorted((g for g, w in signals.genre_affinity.items() if w > 0), key=lambda g: -signals.genre_affinity[g])
    if ranked:
        return ranked[:3], ranked[3:6]
    onboarding = list(signals.onboarding_genre_ids)
    return onboarding[:3], onboarding[3:6]


# Computing a world from scratch is several TMDB round trips (3 parallel
# discover calls, sometimes a fallback and/or padding on top) — several
# seconds worst case. Cached per (user, provider, day): a user reopening the
# same service later the same day (or a friend on the same provider) gets
# an instant cache hit instead of repaying that cost. Not actively
# invalidated on a new review — same staleness tolerance already accepted
# elsewhere in this codebase (recommendations.CACHE_TTL is also 24h) — a
# review made today just doesn't get excluded from today's cached world
# until it's recomputed tomorrow.
_RESULT_CACHE_TTL_HOURS = 24


def get_streaming_world(user_id: str, provider_id: int, day: date | None = None) -> dict:
    """{"popular": [...], "for_you": [...], "different": [...]}, each a list
    of slim movie dicts (same shape as a /discover response's "results") —
    see _compute_streaming_world for how it's built. This wrapper is just
    the Redis read-through cache around that computation."""
    day = day or date.today()
    cache_key = f"streaming_world:{user_id}:{provider_id}:{day.isoformat()}"
    cached = cache.cache_get(cache_key)
    if cached is not None:
        return cached
    result = _compute_streaming_world(user_id, provider_id, day)
    cache.cache_set(cache_key, result, ttl=_RESULT_CACHE_TTL_HOURS * 3600)
    return result


def _compute_streaming_world(user_id: str, provider_id: int, day: date) -> dict:
    """Deduped against each other so the three rows don't just repeat the
    same handful of blockbusters. For You and Different also exclude
    anything the user has already reviewed — Popular doesn't, by design
    (see `already_seen` below)."""
    supabase = get_supabase()
    signals = _load_user_signals(user_id, supabase)
    for_you_genres, different_genres = _affinity_genres(signals)
    profile_terms = build_taste_profile_terms(signals)

    app = current_app._get_current_object()

    # "Popular" deliberately ignores this — same reasoning as Most Popular
    # This Week on the main Discover page: it's a general "what's trending"
    # row, not a personal recommendation, so there's no "already seen" to
    # apply. For You and Different are personal picks, where a film the
    # user has already reviewed has nothing left to offer them.
    already_seen = signals.reviewed_ids

    def _run(kind: str) -> tuple[str, list[dict]]:
        with app.app_context():
            if kind == "popular":
                # No rng here on purpose — Popular is meant to read the same
                # for everyone, a shared "what's trending on this service"
                # fact, not a personalized one.
                return kind, _discover(provider_id, {"sort_by": "popularity.desc"})
            genres = for_you_genres if kind == "for_you" else different_genres
            if not genres:
                return kind, []
            rng = seeded_rng(user_id, day, f"streaming:{provider_id}:{kind}")
            raw = _discover(
                provider_id,
                {"with_genres": "|".join(str(g) for g in genres), "sort_by": rng.choice(_DISCOVER_SORTS)},
                exclude_ids=already_seen,
                rng=rng,
            )
            return kind, _score_and_order(raw, genres, profile_terms, rng)

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
        rng = seeded_rng(user_id, day, f"streaming:{provider_id}:for_you_fallback")
        raw = _discover(provider_id, {"sort_by": "vote_average.desc"}, exclude_ids=already_seen, rng=rng)
        pools["for_you"] = _score_and_order(raw, for_you_genres, profile_terms, rng)
    if not pools["different"]:
        rng = seeded_rng(user_id, day, f"streaming:{provider_id}:different_fallback")
        raw = _discover(provider_id, {"sort_by": "popularity.desc"}, exclude_ids=already_seen, rng=rng)
        pools["different"] = _score_and_order(raw, different_genres, profile_terms, rng)

    seen: set[int] = set()
    out: dict[str, list[dict]] = {}
    for kind in ("popular", "for_you", "different"):
        unique: list[dict] = []
        for m in pools[kind]:
            if m["id"] in seen:
                continue
            seen.add(m["id"])
            unique.append(m)
            if len(unique) >= TARGET_POOL_SIZE:
                break
        out[kind] = unique

    # Every section ends up the same length, even one TMDB genuinely has few
    # matches for — padded with further popularity-sorted results the other
    # sections didn't already claim ("some randoms that don't fit any genres"
    # is an acceptable tail for Different specifically, and this applies the
    # same top-up to any section, since a niche service can leave Popular or
    # For You thin too). One shared filler cursor, so a page already fetched
    # to pad one section is never re-fetched to pad the next.
    filler: list[dict] = []
    filler_page = 2
    deadline = time.monotonic() + _PAD_DEADLINE_SECONDS

    def _next_filler() -> dict | None:
        nonlocal filler_page
        while not filler and filler_page <= _PAD_MAX_PAGE and time.monotonic() < deadline:
            filler.extend(m for m in _discover(provider_id, {"sort_by": "popularity.desc", "page": filler_page}) if m["id"] not in seen)
            filler_page += 1
        return filler.pop(0) if filler else None

    for kind in ("popular", "for_you", "different"):
        exclude_seen = already_seen if kind != "popular" else None
        while len(out[kind]) < TARGET_POOL_SIZE:
            m = _next_filler()
            if m is None:
                break
            seen.add(m["id"])
            # Still consumed from the shared queue either way (it's a
            # one-time list, not re-queryable) — just not handed to a
            # personal section for a film already reviewed.
            if exclude_seen and m["id"] in exclude_seen:
                continue
            out[kind].append(m)

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
