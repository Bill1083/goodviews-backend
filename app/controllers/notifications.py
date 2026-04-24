from flask import Blueprint, jsonify, request

from app import limiter
from app.utils.auth import require_auth
from app.services.supabase_client import get_supabase

notifications_bp = Blueprint("notifications", __name__)


@notifications_bp.get("/recommendations")
@require_auth
@limiter.limit("60 per minute")
def get_recommendations():
    """Return all non-dismissed movie recommendations for the current user."""
    user = request.current_user
    supabase = get_supabase()
    try:
        result = (
            supabase.table("notifications")
            .select(
                "id, movie_id, is_read, dismissed, created_at, sender_id,"
                "movies(id, title, poster_path, release_date)"
            )
            .eq("user_id", str(user.id))
            .eq("dismissed", False)
            .order("created_at", desc=True)
            .execute()
        )

        notifs = result.data

        # Resolve sender usernames and fetch their review for each movie
        sender_ids = list({n["sender_id"] for n in notifs if n.get("sender_id")})
        sender_map: dict = {}
        if sender_ids:
            prof_result = (
                supabase.table("profiles")
                .select("id, username")
                .in_("id", sender_ids)
                .execute()
            )
            sender_map = {p["id"]: p for p in prof_result.data}

        for notif in notifs:
            sid = notif.get("sender_id")
            notif["sender"] = sender_map.get(sid) if sid else None

            # Fetch sender's review for this movie
            if sid and notif.get("movie_id"):
                rev_result = (
                    supabase.table("reviews")
                    .select("id, rating, review_text, created_at")
                    .eq("user_id", sid)
                    .eq("movie_id", notif["movie_id"])
                    .limit(1)
                    .execute()
                )
                notif["sender_review"] = rev_result.data[0] if rev_result.data else None
            else:
                notif["sender_review"] = None

        return jsonify(notifs)
    except Exception as exc:
        return jsonify({"error": "Failed to fetch recommendations", "detail": str(exc)}), 500


@notifications_bp.patch("/<notif_id>/read")
@require_auth
@limiter.limit("120 per minute")
def mark_read(notif_id: str):
    """Mark a recommendation notification as read."""
    user = request.current_user
    supabase = get_supabase()
    try:
        result = (
            supabase.table("notifications")
            .update({"is_read": True})
            .eq("id", notif_id)
            .eq("user_id", str(user.id))
            .execute()
        )
        if not result.data:
            return jsonify({"error": "Notification not found"}), 404
        return jsonify({"updated": True}), 200
    except Exception as exc:
        return jsonify({"error": "Failed to mark read", "detail": str(exc)}), 500


@notifications_bp.patch("/<notif_id>/dismiss")
@require_auth
@limiter.limit("60 per minute")
def dismiss(notif_id: str):
    """Dismiss (and mark read) a recommendation notification."""
    user = request.current_user
    supabase = get_supabase()
    try:
        result = (
            supabase.table("notifications")
            .update({"dismissed": True, "is_read": True})
            .eq("id", notif_id)
            .eq("user_id", str(user.id))
            .execute()
        )
        if not result.data:
            return jsonify({"error": "Notification not found"}), 404
        return jsonify({"dismissed": True}), 200
    except Exception as exc:
        return jsonify({"error": "Failed to dismiss notification", "detail": str(exc)}), 500
