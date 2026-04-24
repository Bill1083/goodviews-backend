from flask import Blueprint, jsonify, request

from app import limiter
from app.utils.auth import require_auth
from app.utils.sanitize import sanitize_text
from app.services import tmdb
from app.services.supabase_client import get_supabase

movies_bp = Blueprint("movies", __name__)


@movies_bp.get("/search")
@limiter.limit("30 per minute")
def search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "Query parameter 'q' is required"}), 400
    if len(query) > 200:
        return jsonify({"error": "Query too long"}), 400

    page = request.args.get("page", 1, type=int)
    page = max(1, min(page, 500))  # TMDB supports up to page 500

    try:
        data = tmdb.search_movies(query, page)
        return jsonify(data)
    except Exception as exc:
        return jsonify({"error": "Failed to fetch movies", "detail": str(exc)}), 502


@movies_bp.get("/<int:movie_id>")
@limiter.limit("60 per minute")
def details(movie_id: int):
    try:
        data = tmdb.get_movie_details(movie_id)
        return jsonify(data)
    except Exception as exc:
        return jsonify({"error": "Failed to fetch movie details", "detail": str(exc)}), 502


@movies_bp.post("/recommend")
@require_auth
@limiter.limit("20 per hour")
def recommend_movie():
    """Send a movie recommendation to individual friends and/or friend groups."""
    user = request.current_user
    body = request.get_json(silent=True) or {}

    movie_id = body.get("movie_id")
    if not movie_id or not isinstance(movie_id, int):
        return jsonify({"error": "Valid movie_id (integer) is required"}), 400

    raw_friend_ids = body.get("friend_ids") or []
    friend_ids = [str(f) for f in raw_friend_ids if f] if isinstance(raw_friend_ids, list) else []
    raw_group_ids = body.get("group_ids") or []
    group_ids = [str(g) for g in raw_group_ids if g] if isinstance(raw_group_ids, list) else []

    movie_data = {
        "id": movie_id,
        "title": sanitize_text(body.get("title", "")),
        "poster_path": body.get("poster_path"),
        "release_date": body.get("release_date"),
    }

    supabase = get_supabase()
    try:
        supabase.table("movies").upsert(movie_data, on_conflict="id").execute()

        recipient_ids: set[str] = set(friend_ids)

        # Expand groups to member user_ids
        for gid in group_ids:
            members = (
                supabase.table("group_members")
                .select("user_id")
                .eq("group_id", gid)
                .execute()
            )
            for m in members.data:
                recipient_ids.add(m["user_id"])

        # Don't notify yourself
        recipient_ids.discard(str(user.id))

        if recipient_ids:
            notif_rows = [
                {
                    "user_id": rid,
                    "sender_id": str(user.id),
                    "movie_id": movie_id,
                    "message": "recommended a movie to you",
                }
                for rid in recipient_ids
            ]
            supabase.table("notifications").insert(notif_rows).execute()

        return jsonify({"message": "Sent", "recipient_count": len(recipient_ids)}), 200
    except Exception as exc:
        return jsonify({"error": "Failed to send recommendation", "detail": str(exc)}), 500
