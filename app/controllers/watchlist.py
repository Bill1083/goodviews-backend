from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from app import limiter
from app.utils.auth import require_auth
from app.utils.sanitize import sanitize_text
from app.services.supabase_client import get_supabase
from app.services import cache, movie_cache
from app.utils.errors import server_error

watchlist_bp = Blueprint("watchlist", __name__)


def _enrich_movies(movies: list[dict], supabase) -> list[dict]:
    """Backfill genre_ids and vote_average for movies missing those fields, using cached TMDB data."""
    enriched = []
    for movie in movies:
        if movie is None:
            enriched.append(movie)
            continue
        needs_genre = movie.get("genre_ids") is None
        needs_vote = movie.get("vote_average") is None
        if needs_genre or needs_vote:
            try:
                # movie_cache.get_movie already persists the core segment to
                # the DB; only merge the fetched values into the response here.
                fresh = movie_cache.get_movie(movie["id"], segments=("core",))
                update: dict = {}
                if needs_genre and fresh.get("genre_ids"):
                    update["genre_ids"] = fresh["genre_ids"]
                if needs_vote and fresh.get("vote_average") is not None:
                    update["vote_average"] = fresh["vote_average"]
                if update:
                    movie = {**movie, **update}
            except Exception:
                pass
        enriched.append(movie)
    return enriched


@watchlist_bp.get("/")
@require_auth
@limiter.limit("60 per minute")
def get_watchlist():
    user = request.current_user
    supabase = get_supabase()
    try:
        result = (
            supabase.table("watchlist")
            .select("movie_id, added_at, movies(id, title, poster_path, release_date, vote_average, genre_ids)")
            .eq("user_id", str(user.id))
            .order("added_at", desc=True)
            .execute()
        )
        items = result.data
        # Enrich movies missing genre_ids or vote_average from TMDB (cached)
        movie_map: dict[int, dict] = {}
        for item in items:
            m = item.get("movies")
            if m and m["id"] not in movie_map:
                movie_map[m["id"]] = m
        enriched_movies = _enrich_movies(list(movie_map.values()), supabase)
        enriched_map = {m["id"]: m for m in enriched_movies if m}
        for item in items:
            if item.get("movies") and item["movies"]["id"] in enriched_map:
                item["movies"] = enriched_map[item["movies"]["id"]]
        return jsonify(items)
    except Exception as exc:
        return server_error("Failed to fetch watchlist", exc, 500)


@watchlist_bp.post("/")
@require_auth
@limiter.limit("30 per minute")
def add_to_watchlist():
    user = request.current_user
    body = request.get_json(silent=True) or {}
    movie_id = body.get("movie_id")
    if not movie_id or not isinstance(movie_id, int):
        return jsonify({"error": "Valid movie_id (integer) is required"}), 400

    # Lightweight fallback stub, used only if the authoritative TMDB-backed
    # fetch below fails (e.g. TMDB unreachable) — keeps adding to the
    # watchlist from hard-failing just because TMDB is down.
    fallback_movie_data = {
        "id": movie_id,
        "title": sanitize_text(body.get("title", "")),
        "poster_path": body.get("poster_path"),
        "release_date": body.get("release_date"),
        "last_viewed_at": datetime.now(timezone.utc).isoformat(),
    }
    raw_genre_ids = body.get("genre_ids") or []
    genre_ids_list = [int(g) for g in raw_genre_ids if isinstance(g, (int, float))] if isinstance(raw_genre_ids, list) else []
    if genre_ids_list:
        fallback_movie_data["genre_ids"] = genre_ids_list
    vote_average = body.get("vote_average")
    if vote_average is not None:
        try:
            fallback_movie_data["vote_average"] = float(vote_average)
        except (ValueError, TypeError):
            pass

    supabase = get_supabase()

    # Route through the segmented read-through cache so the movie record ends
    # up fully persisted (not just the FK-satisfying stub) on the first touch.
    try:
        movie_cache.get_movie(movie_id, segments=("core",))
    except Exception:
        try:
            supabase.table("movies").upsert(fallback_movie_data, on_conflict="id").execute()
        except Exception:
            pass

    try:
        existing = (
            supabase.table("watchlist")
            .select("movie_id")
            .eq("user_id", str(user.id))
            .eq("movie_id", movie_id)
            .execute()
        )
        if existing.data:
            return jsonify({"message": "Already in watchlist"}), 200

        result = (
            supabase.table("watchlist")
            .insert({"user_id": str(user.id), "movie_id": movie_id})
            .execute()
        )
        cache.invalidate_user_stats(str(user.id))
        return jsonify(result.data[0]), 201
    except Exception as exc:
        return server_error("Failed to add to watchlist", exc, 500)


@watchlist_bp.delete("/<int:movie_id>")
@require_auth
@limiter.limit("30 per minute")
def remove_from_watchlist(movie_id: int):
    user = request.current_user
    supabase = get_supabase()
    try:
        supabase.table("watchlist").delete() \
            .eq("user_id", str(user.id)) \
            .eq("movie_id", movie_id) \
            .execute()
        cache.invalidate_user_stats(str(user.id))
        return jsonify({"message": "Removed from watchlist"}), 200
    except Exception as exc:
        return server_error("Failed to remove from watchlist", exc, 500)
