import base64
import json
from functools import wraps

from flask import request, jsonify

from app.services.supabase_client import get_supabase
from app.services.trusted_devices import verify_trusted_device


def _decode_aal(token: str) -> str | None:
    """Reads the `aal` claim from a JWT that has already been signature-
    verified via supabase.auth.get_user() (below) — just re-reading the
    payload here, not re-verifying it."""
    try:
        payload_segment = token.split(".")[1]
        padded = payload_segment + "=" * (-len(payload_segment) % 4)
        return json.loads(base64.urlsafe_b64decode(padded)).get("aal")
    except Exception:
        return None


def _has_verified_mfa_factor(user) -> bool:
    return any(factor.status == "verified" for factor in (user.factors or []))


def _is_trusted_device(user_id: str) -> bool:
    """AAL2-equivalent path for a session that skipped the MFA challenge
    because this browser was remembered (see services/trusted_devices.py) —
    checked via a header so every request re-proves trust, not just login."""
    raw_token = request.headers.get("X-Trusted-Device-Token", "")
    return bool(raw_token) and verify_trusted_device(user_id, raw_token)


def _validate_token(token: str):
    try:
        user_response = get_supabase().auth.get_user(token)
    except Exception:
        return None
    return user_response.user if user_response else None


def require_auth(f):
    """Validates the bearer JWT and, for any account with a verified MFA
    factor enrolled, also requires AAL2 (or a valid trusted-device token) —
    without this, a stolen/guessed password alone would be enough to reach
    every endpoint here, 2FA notwithstanding."""

    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Missing or invalid Authorization header"}), 401

        token = auth_header.split(" ", 1)[1]
        user = _validate_token(token)
        if not user:
            return jsonify({"error": "Invalid or expired token"}), 401

        if _has_verified_mfa_factor(user) and _decode_aal(token) != "aal2":
            if not _is_trusted_device(str(user.id)):
                return jsonify({"error": "MFA verification required"}), 401

        request.current_user = user
        return f(*args, **kwargs)

    return decorated


def require_auth_basic(f):
    """Same JWT validation as require_auth, without the AAL2 requirement.
    Reserved for the handful of pre-challenge endpoints (e.g. checking
    whether this device is already trusted) that must be reachable at AAL1,
    before an MFA-enrolled user has completed their challenge."""

    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Missing or invalid Authorization header"}), 401

        token = auth_header.split(" ", 1)[1]
        user = _validate_token(token)
        if not user:
            return jsonify({"error": "Invalid or expired token"}), 401

        request.current_user = user
        return f(*args, **kwargs)

    return decorated
