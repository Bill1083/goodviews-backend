from flask import Blueprint, jsonify, request

from app import limiter
from app.services.supabase_client import get_supabase

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
        return jsonify({"error": "Failed to resolve user", "detail": str(exc)}), 500

    return jsonify({"email": email})
