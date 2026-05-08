from flask import Blueprint, jsonify, request

from app import limiter
from app.utils.auth import require_auth
from app.utils.sanitize import sanitize_text
from app.services.supabase_client import get_supabase
from app.services import tmdb as tmdb_service

watchlist_bp = Blueprint("watchlist", __name__)


def _enrich_movies(movies: list[dict], supabase) -> list[dict]:
    """Backfill genre_ids and vote_average for movies missing those fields, using cached TMDB data."""
    enriched = []
    db_updates = []
    for movie in movies:
        if movie is None:
            enriched.append(movie)
            continue
        needs_genre = movie.get("genre_ids") is None
        needs_vote = movie.get("vote_average") is None
        if needs_genre or needs_vote:
            try:
                details = tmdb_service.get_movie_details(movie["id"])
                update: dict = {}
                if needs_genre and details.get("genres"):
                    update["genre_ids"] = [g["id"] for g in details["genres"]]
                if needs_vote and details.get("vote_average") is not None:
                    update["vote_average"] = details["vote_average"]
                if update:
                    db_updates.append({"id": movie["id"], **update})
                    movie = {**movie, **update}
            except Exception:
                pass
        enriched.append(movie)
    if db_updates:
        try:
            supabase.table("movies").upsert(db_updates, on_conflict="id").execute()
        except Exception:
            pass
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
        return jsonify({"error": "Failed to fetch watchlist", "detail": str(exc)}), 500


@watchlist_bp.post("/")
@require_auth
@limiter.limit("30 per minute")
def add_to_watchlist():
    user = request.current_user
    body = request.get_json(silent=True) or {}
    movie_id = body.get("movie_id")
    if not movie_id or not isinstance(movie_id, int):
        return jsonify({"error": "Valid movie_id (integer) is required"}), 400

    movie_data = {
        "id": movie_id,
        "title": sanitize_text(body.get("title", "")),
        "poster_path": body.get("poster_path"),
        "release_date": body.get("release_date"),
    }
    raw_genre_ids = body.get("genre_ids") or []
    genre_ids_list = [int(g) for g in raw_genre_ids if isinstance(g, (int, float))] if isinstance(raw_genre_ids, list) else []
    if genre_ids_list:
        movie_data["genre_ids"] = genre_ids_list
    vote_average = body.get("vote_average")
    if vote_average is not None:
        try:
            movie_data["vote_average"] = float(vote_average)
        except (ValueError, TypeError):
            pass

    supabase = get_supabase()
    try:
        supabase.table("movies").upsert(movie_data, on_conflict="id").execute()

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
        return jsonify(result.data[0]), 201
    except Exception as exc:
        return jsonify({"error": "Failed to add to watchlist", "detail": str(exc)}), 500


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
        return jsonify({"message": "Removed from watchlist"}), 200
    except Exception as exc:
        return jsonify({"error": "Failed to remove from watchlist", "detail": str(exc)}), 500
