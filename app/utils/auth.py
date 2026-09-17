import base64
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import request, jsonify

from app.services.supabase_client import get_supabase
from app.services.trusted_devices import verify_trusted_device

logger = logging.getLogger(__name__)

# get_user() doesn't return factors, so enrolment is looked up via the admin
# API instead — cached briefly so it isn't an extra round-trip per request.
# The cost of the TTL is that enrolling MFA takes effect here within a few
# minutes rather than instantly.
_MFA_ENROLMENT_TTL = timedelta(minutes=5)
_mfa_enrolment_cache: dict[str, tuple[bool, datetime]] = {}
_mfa_enrolment_lock = threading.Lock()


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


def _is_mfa_enrolled(user_id: str) -> bool:
    """Whether the account has a verified MFA factor, via the admin API.

    supabase-py's auth.get_user(jwt) leaves `factors` unset, so reading it off
    that response made every MFA-enrolled account look MFA-free — which meant
    the AAL2 requirement below silently never applied and a password-only
    (aal1) session could reach every endpoint. Errors here fall open (and
    aren't cached, so the next call retries) rather than locking the whole
    app out on a transient Supabase blip."""
    now = datetime.now(timezone.utc)
    with _mfa_enrolment_lock:
        cached = _mfa_enrolment_cache.get(user_id)
        if cached and cached[1] > now:
            return cached[0]

    try:
        admin_user = get_supabase().auth.admin.get_user_by_id(user_id).user
    except Exception:
        logger.exception("MFA enrolment lookup failed for %s — allowing the request through", user_id)
        return False

    enrolled = any(getattr(f, "status", None) == "verified" for f in (getattr(admin_user, "factors", None) or []))
    with _mfa_enrolment_lock:
        _mfa_enrolment_cache[user_id] = (enrolled, now + _MFA_ENROLMENT_TTL)
    return enrolled


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

        if _decode_aal(token) != "aal2" and _is_mfa_enrolled(str(user.id)):
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
