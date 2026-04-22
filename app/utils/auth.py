from functools import wraps

from flask import request, jsonify
from supabase import Client

from app.services.supabase_client import get_supabase


def require_auth(f):
    """Decorator that validates the Supabase JWT from the Authorization header."""

    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Missing or invalid Authorization header"}), 401

        token = auth_header.split(" ", 1)[1]
        supabase: Client = get_supabase()

        try:
            user_response = supabase.auth.get_user(token)
            if not user_response or not user_response.user:
                return jsonify({"error": "Invalid or expired token"}), 401
        except Exception:
            return jsonify({"error": "Token validation failed"}), 401

        request.current_user = user_response.user
        return f(*args, **kwargs)

    return decorated
