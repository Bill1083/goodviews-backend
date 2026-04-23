from flask import Blueprint, jsonify, request

from app import limiter
from app.utils.auth import require_auth
from app.utils.sanitize import sanitize_str
from app.services.supabase_client import get_supabase

profile_bp = Blueprint("profile", __name__)

VALID_VISIBILITY = ("no_one", "friends_only", "everyone")


@profile_bp.get("/")
@require_auth
@limiter.limit("60 per minute")
def get_profile():
    user = request.current_user
    supabase = get_supabase()
    try:
        result = (
            supabase.table("profiles")
            .select("id, username, bio, profile_visibility, avatar_color")
            .eq("id", str(user.id))
            .single()
            .execute()
        )
        return jsonify(result.data)
    except Exception as exc:
        return jsonify({"error": "Failed to fetch profile", "detail": str(exc)}), 500


@profile_bp.put("/")
@require_auth
@limiter.limit("20 per minute")
def update_profile():
    user = request.current_user
    body = request.get_json(silent=True) or {}
    supabase = get_supabase()

    updates: dict = {}

    if "username" in body:
        new_username = sanitize_str(body["username"], max_length=50)
        if not new_username:
            return jsonify({"error": "Username cannot be empty"}), 400
        # Check uniqueness
        existing = (
            supabase.table("profiles")
            .select("id")
            .eq("username", new_username)
            .neq("id", str(user.id))
            .execute()
        )
        if existing.data:
            return jsonify({"error": "Username already taken"}), 409
        updates["username"] = new_username

    if "bio" in body:
        updates["bio"] = sanitize_str(body["bio"], max_length=500) if body["bio"] else None

    if "profile_visibility" in body:
        vis = body["profile_visibility"]
        if vis not in VALID_VISIBILITY:
            return jsonify({"error": "Invalid profile_visibility value"}), 400
        updates["profile_visibility"] = vis

    if "avatar_color" in body:
        color = sanitize_str(body["avatar_color"], max_length=7)
        updates["avatar_color"] = color if color else None

    if not updates:
        return jsonify({"error": "No valid fields provided"}), 400

    try:
        result = (
            supabase.table("profiles")
            .update(updates)
            .eq("id", str(user.id))
            .execute()
        )
        return jsonify(result.data[0] if result.data else {})
    except Exception as exc:
        return jsonify({"error": "Failed to update profile", "detail": str(exc)}), 500
