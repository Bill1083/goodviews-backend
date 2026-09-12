from flask import Blueprint, jsonify, request

from app import limiter
from app.services.supabase_client import get_supabase
from app.services import trusted_devices
from app.utils.auth import require_auth
from app.utils.errors import server_error

auth_bp = Blueprint("auth", __name__)


@auth_bp.post("/resolve-login")
@limiter.limit("10 per minute")
def resolve_login():
    """Given a username (non-email), return the associated email address
    so the frontend can sign in via Supabase Auth."""
    body = request.get_json(silent=True) or {}
    login = body.get("login", "").strip()

    if not login:
        return jsonify({"error": "login is required"}), 400

    # If it already looks like an email, just echo it back
    if "@" in login:
        return jsonify({"email": login})

    supabase = get_supabase()

    # Look up the user ID by username
    try:
        result = (
            supabase.table("profiles")
            .select("id")
            .eq("username", login)
            .single()
            .execute()
        )
    except Exception:
        return jsonify({"error": "User not found"}), 404

    if not result.data:
        return jsonify({"error": "User not found"}), 404

    user_id = result.data["id"]

    # Fetch the email via the admin API (service role key required)
    try:
        user_response = supabase.auth.admin.get_user_by_id(user_id)
        email = user_response.user.email
    except Exception as exc:
        return server_error("Failed to resolve user", exc, 500)

    return jsonify({"email": email})


@auth_bp.post("/trusted-devices")
@require_auth
@limiter.limit("10 per minute")
def create_trusted_device():
    """Called right after a successful MFA verify when the user checked
    "remember this device" — issues an opaque token scoped to this account
    that /trusted-devices/verify can later redeem to skip the challenge."""
    try:
        raw_token, expires_at = trusted_devices.create_trusted_device(str(request.current_user.id))
    except Exception as exc:
        return server_error("Failed to create trusted device", exc, 500)
    return jsonify({"token": raw_token, "expires_at": expires_at})


@auth_bp.post("/trusted-devices/verify")
@require_auth
@limiter.limit("30 per minute")
def verify_trusted_device():
    """Called on login (before showing the MFA challenge) with whatever
    trusted-device token the client has stored for this account, if any."""
    body = request.get_json(silent=True) or {}
    token = body.get("token", "")
    if not token:
        return jsonify({"trusted": False})
    try:
        trusted = trusted_devices.verify_trusted_device(str(request.current_user.id), token)
    except Exception as exc:
        return server_error("Failed to verify trusted device", exc, 500)
    return jsonify({"trusted": trusted})
