from flask import Blueprint, current_app, jsonify, request
import requests as http_requests

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
            .select("id, username, bio, profile_visibility, avatar_color, hide_recent_movies")
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

    if "hide_recent_movies" in body:
        updates["hide_recent_movies"] = bool(body["hide_recent_movies"])

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


@profile_bp.delete("/")
@require_auth
@limiter.limit("3 per hour")
def delete_account():
    """Permanently delete the authenticated user's account and all associated data.

    The caller must confirm by supplying their username in the request body.
    Deletion of auth.users cascades to profiles → all related tables (CASCADE).
    """
    user = request.current_user
    body = request.get_json(silent=True) or {}
    supabase = get_supabase()

    confirm_username = sanitize_str(body.get("confirm_username", ""), max_length=50)
    if not confirm_username:
        return jsonify({"error": "confirm_username is required"}), 400

    # Verify the supplied username matches the authenticated user
    try:
        profile_result = (
            supabase.table("profiles")
            .select("username")
            .eq("id", str(user.id))
            .single()
            .execute()
        )
    except Exception as exc:
        return jsonify({"error": "Failed to fetch profile", "detail": str(exc)}), 500

    actual_username = profile_result.data.get("username", "") if profile_result.data else ""
    if confirm_username != actual_username:
        return jsonify({"error": "Username does not match"}), 409

    # Delete the auth user via the Admin REST API — all app data cascades via DB foreign keys
    try:
        supabase_url = current_app.config["SUPABASE_URL"]
        service_key = current_app.config["SUPABASE_SERVICE_ROLE_KEY"]

        resp = http_requests.delete(
            f"{supabase_url}/auth/v1/admin/users/{user.id}",
            headers={
                "apikey": service_key,
                "Authorization": f"Bearer {service_key}",
            },
            timeout=10,
        )
        if resp.status_code not in (200, 204):
            error_msg = resp.json().get("message", resp.text) if resp.text else "Unknown error"
            return jsonify({"error": "Failed to delete account", "detail": error_msg}), 500
    except Exception as exc:
        return jsonify({"error": "Failed to delete account", "detail": str(exc)}), 500

    return jsonify({"message": "Account deleted"}), 200
