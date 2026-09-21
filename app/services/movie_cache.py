import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

from flask import current_app

from app.services import tmdb
from app.services.pg import chunked, paginate
from app.services.supabase_client import get_supabase

logger = logging.getLogger(__name__)

ALL_SEGMENTS = ("core", "media", "providers")

# TMDB's genre list is a small, essentially-static ~19-entry set. The `movies`
# table only stores genre_ids (int array), so the frontend's full
# {id, name} genres shape is reconstructed from this map rather than adding
# a redundant genre-name column.
GENRE_MAP = {
    28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime",
    99: "Documentary", 18: "Drama", 10751: "Family", 14: "Fantasy", 36: "History",
    27: "Horror", 10402: "Music", 9648: "Mystery", 10749: "Romance",
    878: "Science Fiction", 10770: "TV Movie", 53: "Thriller", 10752: "War", 37: "Western",
}

# Slim, stats-friendly columns added by sql/008. Written on every core-segment
# fetch from the same /movie/{id} payload as everything else (no extra TMDB
# calls); read by app/services/stats.py. If PostgREST reports them missing
# (migration not applied yet), _persist retries the write without them.
STATS_COLUMNS = (
    "directors", "top_cast", "original_language", "production_countries",
    "budget", "revenue", "popularity", "vote_count",
    "collection_id", "collection_name", "tagline",
)

TOP_CAST_LIMIT = 10


def _segment_ttls() -> dict[str, timedelta]:
    cfg = current_app.config
    return {
        "core": timedelta(days=cfg["MOVIE_CORE_TTL_DAYS"]),
        "media": timedelta(days=cfg["MOVIE_MEDIA_TTL_DAYS"]),
        "providers": timedelta(hours=cfg["MOVIE_PROVIDERS_TTL_HOURS"]),
    }


def _parse_ts(value: str) -> datetime:
    ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _stale_segments(row: dict | None, requested: tuple[str, ...]) -> list[str]:
    if row is None:
        return list(requested)
    ttls = _segment_ttls()
    now = datetime.now(timezone.utc)
    stale = []
    for seg in requested:
        raw_ts = row.get(f"{seg}_updated_at")
        if not raw_ts or _parse_ts(raw_ts) + ttls[seg] < now:
            stale.append(seg)
    return stale


def _slim_person(person: dict) -> dict:
    return {
        "id": person.get("id"),
        "name": person.get("name"),
        "profile_path": person.get("profile_path"),
    }


def extract_people(credits: Any) -> tuple[list[dict] | None, list[dict] | None]:
    """(directors, top_cast) in the slim shape stored beside the raw credits
    jsonb — see sql/008 for why. Both are None when there is no credits
    object at all, so "never fetched" stays distinguishable from "fetched,
    nobody credited" ([])."""
    if not isinstance(credits, dict):
        return None, None
    crew = credits.get("crew") or []
    cast = credits.get("cast") or []
    directors = [
        _slim_person(c) for c in crew
        if isinstance(c, dict) and c.get("job") == "Director" and c.get("id")
    ]
    billed = sorted(
        (c for c in cast if isinstance(c, dict) and c.get("id")),
        key=lambda c: c["order"] if isinstance(c.get("order"), int) else 10**6,
    )
    return directors, [_slim_person(c) for c in billed[:TOP_CAST_LIMIT]]


def _extract_segment_fields(seg: str, tmdb_data: dict) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    if seg == "core":
        genres = tmdb_data.get("genres") or []
        directors, top_cast = extract_people(tmdb_data.get("credits"))
        collection = tmdb_data.get("belongs_to_collection")
        if not isinstance(collection, dict):
            collection = {}
        countries = tmdb_data.get("production_countries") or []
        return {
            "title": tmdb_data.get("title"),
            "poster_path": tmdb_data.get("poster_path"),
            "release_date": tmdb_data.get("release_date"),
            "vote_average": tmdb_data.get("vote_average"),
            "genre_ids": [g["id"] for g in genres] or None,
            "overview": tmdb_data.get("overview"),
            "runtime": tmdb_data.get("runtime"),
            "credits": tmdb_data.get("credits"),
            # Stats columns (sql/008) — same payload, no extra TMDB calls.
            "directors": directors,
            "top_cast": top_cast,
            "original_language": tmdb_data.get("original_language") or None,
            "production_countries": [
                c["iso_3166_1"] for c in countries if isinstance(c, dict) and c.get("iso_3166_1")
            ],
            # TMDB reports 0 for "unknown" — store NULL so averages aren't dragged down.
            "budget": tmdb_data.get("budget") or None,
            "revenue": tmdb_data.get("revenue") or None,
            "popularity": tmdb_data.get("popularity"),
            "vote_count": tmdb_data.get("vote_count"),
            "collection_id": collection.get("id"),
            "collection_name": collection.get("name"),
            "tagline": tmdb_data.get("tagline") or None,
            "core_updated_at": now,
        }
    if seg == "media":
        return {
            "backdrop_path": tmdb_data.get("backdrop_path"),
            "videos": tmdb_data.get("videos"),
            "media_updated_at": now,
        }
    if seg == "providers":
        return {
            "watch_providers": tmdb_data.get("watch/providers"),
            "providers_updated_at": now,
        }
    return {}


def _to_response(row: dict) -> dict[str, Any]:
    genre_ids = row.get("genre_ids") or []
    return {
        "id": row["id"],
        "title": row.get("title"),
        "poster_path": row.get("poster_path"),
        "release_date": row.get("release_date"),
        "vote_average": row.get("vote_average"),
        "genre_ids": genre_ids,
        "genres": [{"id": gid, "name": GENRE_MAP.get(gid, "")} for gid in genre_ids],
        "overview": row.get("overview"),
        "runtime": row.get("runtime"),
        "backdrop_path": row.get("backdrop_path"),
        "credits": row.get("credits") or {"cast": [], "crew": []},
        "videos": row.get("videos"),
        # DB column -> TMDB's literal slash key, for frontend compatibility
        # (client/src/types/index.ts: MovieDetails['watch/providers']).
        "watch/providers": row.get("watch_providers"),
    }


def _is_missing_stats_column(exc: Exception) -> bool:
    """PostgREST answers an UPDATE/UPSERT naming an unknown column with
    PGRST204 ("Could not find the 'x' column of 'movies' in the schema cache")."""
    message = str(exc)
    return "PGRST204" in message or (
        "column" in message.lower() and any(col in message for col in STATS_COLUMNS)
    )


def _persist(supabase, movie_id: int, update: dict, insert: bool) -> None:
    """Writes a movie row. If the sql/008 stats columns don't exist yet, logs
    loudly and retries without them — a missed migration must degrade to
    "no taste stats", not break every movie click-through in the app."""

    def _write(payload: dict) -> None:
        if insert:
            # Insert path: upsert needs the NOT NULL columns present (guaranteed by the caller).
            supabase.table("movies").upsert(payload, on_conflict="id").execute()
        else:
            # Update path: a plain UPDATE only touches the columns we pass, so a
            # partial payload (e.g. just last_viewed_at on an all-fresh row) can't
            # trip NOT NULL constraints on columns we're not even setting — unlike
            # upsert(), which validates a full candidate row before honoring the
            # ON CONFLICT clause.
            supabase.table("movies").update(payload).eq("id", movie_id).execute()

    try:
        _write(update)
    except Exception as exc:
        if not any(col in update for col in STATS_COLUMNS) or not _is_missing_stats_column(exc):
            raise
        logger.error(
            "movies table is missing the sql/008 stats columns - apply the migration. "
            "Writing movie %s without them.", movie_id,
        )
        _write({k: v for k, v in update.items() if k not in STATS_COLUMNS})


def get_movie(movie_id: int, segments: tuple[str, ...] = ALL_SEGMENTS) -> dict:
    """Read-through cache: check the DB row first; fetch+persist only the
    requested segments that are missing or past their TTL; return the merged,
    response-shaped dict. This is the click-through permanent-storage entrypoint.
    Every call stamps last_viewed_at, so a movie that's viewed often but never
    needs a TMDB refetch still isn't treated as prune-eligible."""
    supabase = get_supabase()
    existing = supabase.table("movies").select("*").eq("id", movie_id).limit(1).execute()
    row = existing.data[0] if existing.data else None

    stale = _stale_segments(row, segments)
    if row is None and "core" not in stale:
        # First-ever insert must always populate the NOT NULL columns (title
        # etc.), which only the core segment provides — guarantee it's fetched
        # even if the caller only asked for e.g. ("providers",).
        stale = ["core", *stale]

    update: dict = {"id": movie_id, "last_viewed_at": datetime.now(timezone.utc).isoformat()}
    if stale:
        tmdb_data = tmdb.fetch_movie_segments(movie_id, set(stale))
        for seg in stale:
            update.update(_extract_segment_fields(seg, tmdb_data))

    _persist(supabase, movie_id, update, insert=row is None)

    # A genuinely invalid movie_id already raises inside fetch_movie_segments
    # (TMDB 404 -> requests.raise_for_status()) before we get here, so by this
    # point `row` always represents a real movie.
    row = {**(row or {}), **update}
    return _to_response(row)


def force_refresh_movie(movie_id: int) -> dict:
    """Bypasses all TTL/cache checks: always fetches every segment fresh from
    TMDB in one call, overwrites the DB record (all fields + all three
    timestamps), and returns the fresh response. Rate-limiting against abuse
    is enforced at the Flask route (flask-limiter), not here."""
    tmdb_data = tmdb.fetch_movie_segments(movie_id, set(ALL_SEGMENTS))
    update: dict = {"id": movie_id, "last_viewed_at": datetime.now(timezone.utc).isoformat()}
    for seg in ALL_SEGMENTS:
        update.update(_extract_segment_fields(seg, tmdb_data))
    _persist(get_supabase(), movie_id, update, insert=True)
    return _to_response(update)


def _force_refresh_many(movie_ids: list[int]) -> int:
    """Force-refreshes each id from TMDB on a 10-worker pool; returns how many
    succeeded. Each worker thread pushes its own Flask app context —
    force_refresh_movie relies on current_app (TMDB API key), which a
    ThreadPoolExecutor worker doesn't inherit by default."""
    if not movie_ids:
        return 0
    app = current_app._get_current_object()

    def _refresh_one(mid: int) -> bool:
        with app.app_context():
            try:
                force_refresh_movie(mid)
                return True
            except Exception:
                logger.exception("Failed to refresh movie %s", mid)
                return False

    with ThreadPoolExecutor(max_workers=10) as executor:
        return sum(1 for ok in executor.map(_refresh_one, movie_ids) if ok)


def prune_unwatched_movies(days: int | None = None) -> int:
    """Delete movie rows that haven't been viewed in `days` (default
    MOVIE_PRUNE_AFTER_DAYS) AND aren't referenced by any watchlist, review,
    notification, or dismissal row — i.e. pure cache bloat from a movie someone looked at
    once (via search click-through) and never watchlisted or reviewed.
    Rows with no last_viewed_at yet (e.g. only ever touched via
    POST /recommend, which is untouched by this refactor) are left alone
    rather than guessed at — deletion only happens for rows we know were
    genuinely stale. Returns the number of rows deleted."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days or current_app.config["MOVIE_PRUNE_AFTER_DAYS"])
    supabase = get_supabase()

    stale_rows = (
        supabase.table("movies")
        .select("id")
        .lt("last_viewed_at", cutoff.isoformat())
        .execute()
    )
    candidate_ids = [m["id"] for m in stale_rows.data]
    if not candidate_ids:
        return 0

    referenced_ids: set[int] = set()
    # dismissed_recommendations cascades on movie delete — pruning would silently
    # un-dismiss the movie and erase any "types" preference attached to it.
    for table in ("watchlist", "reviews", "notifications", "dismissed_recommendations"):
        refs = supabase.table(table).select("movie_id").in_("movie_id", candidate_ids).execute()
        referenced_ids.update(r["movie_id"] for r in refs.data if r.get("movie_id") is not None)

    to_delete = [mid for mid in candidate_ids if mid not in referenced_ids]
    if not to_delete:
        return 0

    supabase.table("movies").delete().in_("id", to_delete).execute()
    logger.info("Pruned %d unwatched movie(s) older than %s days", len(to_delete), days or current_app.config["MOVIE_PRUNE_AFTER_DAYS"])
    return len(to_delete)


def refresh_stale_movies(max_age_days: int | None = None) -> int:
    """TMDB's API Terms of Use (Section 1.C) prohibit caching TMDB-sourced
    information for longer than 6 months. A movie referenced by a review or
    watchlist item is never pruned by prune_unwatched_movies, so without this
    it could sit unrefreshed indefinitely if nobody ever revisits it. Finds
    every row past the cutoff (or that never got a real core fetch at all —
    e.g. a fallback stub inserted while TMDB was briefly down) and
    force-refreshes it. Intended to run on the same kind of schedule as
    prune-movies (see app/__init__.py's CLI commands) — monthly is
    comfortably inside the default 150-day margin. Returns the number of
    rows successfully refreshed."""
    cutoff = datetime.now(timezone.utc) - timedelta(
        days=max_age_days or current_app.config["MOVIE_MAX_CACHE_AGE_DAYS"]
    )
    supabase = get_supabase()

    stale_rows = (
        supabase.table("movies")
        .select("id")
        .or_(f"core_updated_at.is.null,core_updated_at.lt.{cutoff.isoformat()}")
        .execute()
    )
    stale_ids = [m["id"] for m in stale_rows.data]
    if not stale_ids:
        return 0

    refreshed = _force_refresh_many(stale_ids)
    logger.info(
        "Refreshed %d/%d stale movie(s) older than %s days",
        refreshed, len(stale_ids), max_age_days or current_app.config["MOVIE_MAX_CACHE_AGE_DAYS"],
    )
    return refreshed


def backfill_movie_extras(limit: int | None = None) -> int:
    """One-off after applying sql/008: re-fetches from TMDB every movie that
    someone has reviewed or watchlisted and whose stats columns were never
    written (original_language IS NULL — TMDB always supplies it, so NULL
    means "last written by pre-008 code"). Only referenced movies: the rest
    are cache bloat that prune-movies deletes anyway, and the monthly
    refresh-stale-movies job fills them in over time regardless. Safe to
    re-run (failures stay NULL and are retried); `limit` allows incremental
    runs. Returns the number of movies refreshed."""
    supabase = get_supabase()

    referenced: set[int] = set()
    for table in ("reviews", "watchlist"):
        rows = paginate(lambda t=table: supabase.table(t).select("movie_id").order("movie_id"))
        referenced.update(r["movie_id"] for r in rows if r.get("movie_id") is not None)
    if not referenced:
        return 0

    missing: list[int] = []
    for chunk in chunked(sorted(referenced)):
        result = (
            supabase.table("movies")
            .select("id")
            .in_("id", chunk)
            .is_("original_language", "null")
            .execute()
        )
        missing.extend(m["id"] for m in result.data)
    if limit:
        missing = missing[:limit]
    if not missing:
        logger.info("Extras backfill: nothing to do")
        return 0

    refreshed = 0
    for chunk in chunked(missing, 100):
        refreshed += _force_refresh_many(chunk)
        logger.info("Extras backfill: %d/%d refreshed so far", refreshed, len(missing))
    return refreshed


def get_movie_images(movie_id: int) -> dict:
    """Thin pass-through to tmdb.get_movie_images, so controllers only ever
    import movie_cache for movie_id-keyed data rather than app.services.tmdb
    directly. The images gallery stays Redis-backed — see tmdb.py."""
    return tmdb.get_movie_images(movie_id)
