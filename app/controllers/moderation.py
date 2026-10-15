from flask import Blueprint, jsonify, request

from app import limiter
from app.services import moderation
from app.services.supabase_client import get_supabase
from app.utils.auth import require_admin, require_auth_allow_blocked
from app.utils.errors import server_error

moderation_bp = Blueprint("moderation", __name__)
admin_bp = Blueprint("admin", __name__)


@moderation_bp.get("/me")
@require_auth_allow_blocked
@limiter.limit("60 per minute")
def my_moderation_state():
    """The caller's own moderation status - also how the client learns it is
    blocked (every other endpoint answers 403 account_blocked) and whether to
    show the admin page."""
    return jsonify(moderation.public_state(request.moderation_state))


@admin_bp.get("/overview")
@require_admin
@limiter.limit("60 per minute")
def overview():
    try:
        return jsonify(moderation.overview(get_supabase()))
    except Exception as exc:
        return server_error("Failed to load moderation overview", exc, 500)


def _run(action):
    """Runs an admin action, mapping ModerationError to its HTTP status."""
    try:
        action()
    except moderation.ModerationError as exc:
        return jsonify({"error": exc.message}), exc.status
    except Exception as exc:
        return server_error("Moderation action failed", exc, 500)
    return jsonify({"ok": True})


@admin_bp.post("/users/<user_id>/lift-suspension")
@require_admin
@limiter.limit("60 per minute")
def lift_suspension(user_id: str):
    body = request.get_json(silent=True) or {}
    admin_id = str(request.current_user.id)
    return _run(lambda: moderation.lift_suspension(get_supabase(), admin_id, user_id, body.get("warnings")))


@admin_bp.post("/users/<user_id>/warnings")
@require_admin
@limiter.limit("60 per minute")
def set_warnings(user_id: str):
    body = request.get_json(silent=True) or {}
    admin_id = str(request.current_user.id)
    return _run(lambda: moderation.set_warnings(get_supabase(), admin_id, user_id, body.get("warnings")))


@admin_bp.post("/deletion-requests/<request_id>/approve")
@require_admin
@limiter.limit("30 per minute")
def approve_deletion(request_id: str):
    admin_id = str(request.current_user.id)
    return _run(lambda: moderation.approve_deletion(get_supabase(), admin_id, request_id))


@admin_bp.post("/deletion-requests/<request_id>/reject")
@require_admin
@limiter.limit("30 per minute")
def reject_deletion(request_id: str):
    """Body: {"warnings": 0 | 1 | 2} - how many warnings the restored account keeps."""
    body = request.get_json(silent=True) or {}
    admin_id = str(request.current_user.id)
    return _run(lambda: moderation.reject_deletion(get_supabase(), admin_id, request_id, body.get("warnings")))
