import re

from flask import Blueprint, current_app, jsonify, request
import requests as http_requests

from app import limiter
from app.utils.auth import require_auth
from app.utils.sanitize import sanitize_str
from app.utils.social import are_friends, load_friends_of, load_viewable_profile
from app.services.supabase_client import get_supabase
from app.services.movie_cache import GENRE_MAP
from app.utils.errors import server_error

profile_bp = Blueprint("profile", __name__)

VALID_VISIBILITY = ("no_one", "friends_only", "everyone")

# The caller's own row: everything the settings screen can edit.
SELF_PROFILE_COLUMNS = (
    "id, username, bio, profile_visibility, avatar_color, avatar_url, avatar_focal_y, avatar_zoom, hide_recent_movies, hide_friends_list, mute_recommendations, mute_friend_requests, has_onboarded, onboarding_genre_ids"
)
LEGACY_SELF_PROFILE_COLUMNS = (
    "id, username, bio, profile_visibility, avatar_color, avatar_url, avatar_focal_y, avatar_zoom, hide_recent_movies, mute_recommendations, mute_friend_requests, has_onboarded, onboarding_genre_ids"
)

# Avatars are picked from TMDB movie-poster art (see AvatarPicker on the client) rather than
# uploaded, so we only ever need to accept TMDB's own image URLs here.
_AVATAR_URL_RE = re.compile(r"^https://image\.tmdb\.org/t/p/\w+/[A-Za-z0-9]+\.(jpg|jpeg|png)$")


@profile_bp.get("/")
@require_auth
@limiter.limit("60 per minute")
def get_profile():
    user = request.current_user
    supabase = get_supabase()

    def read(columns: str):
        return (
            supabase.table("profiles")
            .select(columns)
            .eq("id", str(user.id))
            .single()
            .execute()
        )

    try:
        try:
            result = read(SELF_PROFILE_COLUMNS)
        except Exception as exc:
            # sql/009 not applied yet — serve the profile without the new flag
            # rather than breaking every page that reads it.
            if "hide_friends_list" not in str(exc):
                raise
            result = read(LEGACY_SELF_PROFILE_COLUMNS)
        return jsonify(result.data)
    except Exception as exc:
        return server_error("Failed to fetch profile", exc, 500)


@profile_bp.get("/<user_id>")
@require_auth
@limiter.limit("60 per minute")
def get_public_profile(user_id: str):
    """Someone else's profile, as far as their privacy settings allow.

    profile_visibility decides whether the profile opens at all (this is the
    release that starts enforcing it); hide_friends_list decides whether the
    friends list comes with it. Nothing here is writable, and the private half
    of the row — mute flags, onboarding state — is never selected."""
    viewer = request.current_user
    supabase = get_supabase()
    try:
        row, outcome = load_viewable_profile(supabase, str(viewer.id), user_id)
        if outcome == "not_found":
            return jsonify({"error": "Profile not found"}), 404
        if outcome == "private":
            # Deliberately no username/avatar in this response: a profile set to
            # "no one" shouldn't confirm anything beyond its own existence.
            return jsonify({"error": "private"}), 403

        is_self = str(user_id) == str(viewer.id)
        is_friend = False if is_self else are_friends(supabase, str(viewer.id), user_id)
        # None (rather than []) means "they've hidden this", which the client
        # renders as no section at all rather than an empty one.
        friends = None if row.get("hide_friends_list") else load_friends_of(supabase, user_id)
        return jsonify({
            "id": row["id"],
            "username": row.get("username"),
            "bio": row.get("bio"),
            "avatar_url": row.get("avatar_url"),
            "avatar_color": row.get("avatar_color"),
            "avatar_focal_y": row.get("avatar_focal_y"),
            "avatar_zoom": row.get("avatar_zoom"),
            "is_self": is_self,
            "is_friend": is_friend,
            "friends": friends,
            "friend_count": None if friends is None else len(friends),
        })
    except Exception as exc:
        return server_error("Failed to fetch profile", exc, 500)


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

    if "avatar_url" in body:
        url = body["avatar_url"]
        if not url:
            updates["avatar_url"] = None
        else:
            url = sanitize_str(str(url), max_length=300)
            if not _AVATAR_URL_RE.match(url):
                return jsonify({"error": "Invalid avatar_url"}), 400
            updates["avatar_url"] = url

    if "avatar_focal_y" in body:
        try:
            focal_y = int(body["avatar_focal_y"])
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid avatar_focal_y"}), 400
        if not 0 <= focal_y <= 100:
            return jsonify({"error": "avatar_focal_y must be between 0 and 100"}), 400
        updates["avatar_focal_y"] = focal_y

    if "avatar_zoom" in body:
        try:
            zoom = round(float(body["avatar_zoom"]), 2)
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid avatar_zoom"}), 400
        if not 1.0 <= zoom <= 2.5:
            return jsonify({"error": "avatar_zoom must be between 1.0 and 2.5"}), 400
        updates["avatar_zoom"] = zoom

    if "hide_recent_movies" in body:
        updates["hide_recent_movies"] = bool(body["hide_recent_movies"])

    if "hide_friends_list" in body:
        updates["hide_friends_list"] = bool(body["hide_friends_list"])

    if "mute_recommendations" in body:
        updates["mute_recommendations"] = bool(body["mute_recommendations"])

    if "mute_friend_requests" in body:
        updates["mute_friend_requests"] = bool(body["mute_friend_requests"])

    if "has_onboarded" in body:
        updates["has_onboarded"] = bool(body["has_onboarded"])

    if "onboarding_genre_ids" in body:
        raw_genre_ids = body["onboarding_genre_ids"]
        if not isinstance(raw_genre_ids, list):
            return jsonify({"error": "onboarding_genre_ids must be a list"}), 400
        try:
            genre_ids = [int(g) for g in raw_genre_ids]
        except (TypeError, ValueError):
            return jsonify({"error": "onboarding_genre_ids must be integers"}), 400
        if any(g not in GENRE_MAP for g in genre_ids):
            return jsonify({"error": "Invalid genre id in onboarding_genre_ids"}), 400
        updates["onboarding_genre_ids"] = genre_ids

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
        return server_error("Failed to update profile", exc, 500)


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
        return server_error("Failed to fetch profile", exc, 500)

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
        return server_error("Failed to delete account", exc, 500)

    return jsonify({"message": "Account deleted"}), 200
