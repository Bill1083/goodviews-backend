from flask import Blueprint, jsonify, request

from app import limiter
from app.utils.auth import require_auth
from app.utils.sanitize import sanitize_text
from app.services.supabase_client import get_supabase

favourites_bp = Blueprint("favourites", __name__)


# ─── Favourite Actors ─────────────────────────────────────────────────────────

@favourites_bp.get("/actors")
@require_auth
@limiter.limit("60 per minute")
def get_favourite_actors():
    user = request.current_user
    supabase = get_supabase()
    try:
        result = (
            supabase.table("favorite_actors")
            .select("actor_id, actor_name, profile_path")
            .eq("user_id", str(user.id))
            .order("actor_name")
            .execute()
        )
        return jsonify(result.data), 200
    except Exception as exc:
        return jsonify({"error": "Failed to fetch favourite actors", "detail": str(exc)}), 500


@favourites_bp.post("/actors")
@require_auth
@limiter.limit("30 per hour")
def add_favourite_actor():
    user = request.current_user
    body = request.get_json(silent=True) or {}

    person_id = body.get("person_id")
    name = sanitize_text(body.get("name", ""))
    profile_path = body.get("profile_path")

    if not person_id or not isinstance(person_id, int):
        return jsonify({"error": "Valid person_id (integer) is required"}), 400
    if not name:
        return jsonify({"error": "name is required"}), 400

    supabase = get_supabase()
    try:
        supabase.table("favorite_actors").upsert(
            {
                "user_id": str(user.id),
                "actor_id": person_id,
                "actor_name": name,
                "profile_path": profile_path,
            },
            on_conflict="user_id,actor_id",
        ).execute()
        return jsonify({"ok": True}), 201
    except Exception as exc:
        return jsonify({"error": "Failed to add favourite actor", "detail": str(exc)}), 500


@favourites_bp.delete("/actors/<int:person_id>")
@require_auth
@limiter.limit("60 per hour")
def remove_favourite_actor(person_id: int):
    user = request.current_user
    supabase = get_supabase()
    try:
        supabase.table("favorite_actors").delete().eq("user_id", str(user.id)).eq("actor_id", person_id).execute()
        return jsonify({"ok": True}), 200
    except Exception as exc:
        return jsonify({"error": "Failed to remove favourite actor", "detail": str(exc)}), 500


# ─── Favourite Directors ──────────────────────────────────────────────────────

@favourites_bp.get("/directors")
@require_auth
@limiter.limit("60 per minute")
def get_favourite_directors():
    user = request.current_user
    supabase = get_supabase()
    try:
        result = (
            supabase.table("favorite_directors")
            .select("director_id, director_name, profile_path")
            .eq("user_id", str(user.id))
            .order("director_name")
            .execute()
        )
        return jsonify(result.data), 200
    except Exception as exc:
        return jsonify({"error": "Failed to fetch favourite directors", "detail": str(exc)}), 500


@favourites_bp.post("/directors")
@require_auth
@limiter.limit("30 per hour")
def add_favourite_director():
    user = request.current_user
    body = request.get_json(silent=True) or {}

    person_id = body.get("person_id")
    name = sanitize_text(body.get("name", ""))
    profile_path = body.get("profile_path")

    if not person_id or not isinstance(person_id, int):
        return jsonify({"error": "Valid person_id (integer) is required"}), 400
    if not name:
        return jsonify({"error": "name is required"}), 400

    supabase = get_supabase()
    try:
        supabase.table("favorite_directors").upsert(
            {
                "user_id": str(user.id),
                "director_id": person_id,
                "director_name": name,
                "profile_path": profile_path,
            },
            on_conflict="user_id,director_id",
        ).execute()
        return jsonify({"ok": True}), 201
    except Exception as exc:
        return jsonify({"error": "Failed to add favourite director", "detail": str(exc)}), 500


@favourites_bp.delete("/directors/<int:person_id>")
@require_auth
@limiter.limit("60 per hour")
def remove_favourite_director(person_id: int):
    user = request.current_user
    supabase = get_supabase()
    try:
        supabase.table("favorite_directors").delete().eq("user_id", str(user.id)).eq("director_id", person_id).execute()
        return jsonify({"ok": True}), 200
    except Exception as exc:
        return jsonify({"error": "Failed to remove favourite director", "detail": str(exc)}), 500
