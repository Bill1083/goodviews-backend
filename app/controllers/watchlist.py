from flask import Blueprint, jsonify, request

from app import limiter
from app.utils.auth import require_auth
from app.utils.sanitize import sanitize_text
from app.services.supabase_client import get_supabase

watchlist_bp = Blueprint("watchlist", __name__)


@watchlist_bp.get("/")
@require_auth
@limiter.limit("60 per minute")
def get_watchlist():
    user = request.current_user
    supabase = get_supabase()
    try:
        result = (
            supabase.table("watchlist")
            .select("movie_id, added_at, movies(id, title, poster_path, release_date)")
            .eq("user_id", str(user.id))
            .order("added_at", desc=True)
            .execute()
        )
        return jsonify(result.data)
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
