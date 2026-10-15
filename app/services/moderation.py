"""Review moderation: strikes, suspensions, blocks and the admin deletion queue.

Strike ladder, driven by profiles.warning_count:
    1 -> warning             (account stays active)
    2 -> 7-day suspension    (cannot post/edit reviews until suspended_until)
    3 -> blocked             (no access; a deletion request waits for an admin)
An admin can lift a suspension early, set warnings directly, approve the
deletion, or reject it and hand the account back with 0, 1 or 2 warnings.

Every read here fails OPEN: if the moderation columns/tables don't exist yet
(sql/014 not applied) or the database hiccups, nobody is suspended or blocked
- a moderation outage must never lock real users out of the site."""
import logging
from datetime import datetime, timedelta, timezone

import requests as http_requests
from flask import current_app

from app.services import profanity

logger = logging.getLogger(__name__)

SUSPENSION_DAYS = 7
MAX_RESTORE_WARNINGS = 2
FLAGGED_TEXT_MAX_LEN = 500

STATE_COLUMNS = "id, username, warning_count, moderation_status, suspended_until, is_admin"

REVIEW_REMOVED = "Your comment was removed because it broke our community guidelines (hate speech or strong profanity)."


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def detect(text: str | None) -> tuple[str, str] | None:
    """(category, matched_word) if `text` should be removed, else None."""
    return profanity.check(text)


# ─── State ──────────────────────────────────────────────────────────────────
def get_state(supabase, user_id: str) -> dict | None:
    """The user's moderation state, with an expired suspension lifted. None if
    it can't be read (callers treat that as 'no restrictions')."""
    try:
        rows = supabase.table("profiles").select(STATE_COLUMNS).eq("id", user_id).limit(1).execute().data
    except Exception:
        logger.warning("Moderation state unavailable for %s (is sql/014 applied?)", user_id, exc_info=True)
        return None
    if not rows:
        return None
    state = dict(rows[0])

    if state.get("moderation_status") == "suspended":
        until = _parse(state.get("suspended_until"))
        if until is None or until <= _now():
            try:
                supabase.table("profiles").update(
                    {"moderation_status": "active", "suspended_until": None}
                ).eq("id", user_id).execute()
            except Exception:
                logger.exception("Failed to lift expired suspension for %s", user_id)
            state["moderation_status"] = "active"
            state["suspended_until"] = None
    return state


def public_state(state: dict | None) -> dict:
    state = state or {}
    return {
        "status": state.get("moderation_status") or "active",
        "warning_count": state.get("warning_count") or 0,
        "suspended_until": state.get("suspended_until"),
        "is_admin": bool(state.get("is_admin")),
    }


def posting_block(state: dict | None) -> tuple[dict, int] | None:
    """(json body, http status) if this account may not post reviews right now."""
    if not state:
        return None
    status = state.get("moderation_status")
    if status == "blocked":
        return {"error": "Your account is blocked.", "code": "account_blocked"}, 403
    if status == "suspended":
        return {
            "error": "Your account is suspended, so you can't post or edit reviews for now.",
            "code": "account_suspended",
            "suspended_until": state.get("suspended_until"),
        }, 403
    return None


# ─── Strikes ────────────────────────────────────────────────────────────────
def _log_event(supabase, **fields) -> None:
    try:
        supabase.table("moderation_events").insert(fields).execute()
    except Exception:
        logger.exception("Failed to write moderation event %s", fields.get("action"))


def record_violation(supabase, user_id: str, *, category: str, text: str, review_id: str | None = None) -> dict:
    """Applies the next strike and returns what to tell the user:
    {action, warning_count, suspended_until, message}. The offending text is
    kept (truncated) in the audit log so an admin can see what was said."""
    state = get_state(supabase, user_id) or {}
    username = state.get("username")
    strikes = int(state.get("warning_count") or 0) + 1

    update: dict = {"warning_count": strikes}
    suspended_until = None
    if strikes == 1:
        action = "warning"
        message = f"{REVIEW_REMOVED} This is a warning. If it happens again your account will be suspended."
    elif strikes == 2:
        action = "suspension"
        suspended_until = (_now() + timedelta(days=SUSPENSION_DAYS)).isoformat()
        update.update(moderation_status="suspended", suspended_until=suspended_until)
        message = (
            f"{REVIEW_REMOVED} Your account is suspended for {SUSPENSION_DAYS} days and you can't post "
            "reviews until then. One more and your account will be blocked."
        )
    else:
        action = "block"
        update.update(moderation_status="blocked", suspended_until=None)
        message = (
            f"{REVIEW_REMOVED} Your account has been blocked. An admin will review it and decide "
            "whether it is deleted or restored."
        )

    try:
        supabase.table("profiles").update(update).eq("id", user_id).execute()
    except Exception:
        logger.exception("Failed to apply moderation strike to %s", user_id)

    _log_event(
        supabase,
        user_id=user_id,
        username=username,
        action=action,
        category=category,
        flagged_text=text[:FLAGGED_TEXT_MAX_LEN],
        review_id=review_id,
    )
    if action == "block":
        _open_deletion_request(supabase, user_id, username)

    return {"action": action, "warning_count": strikes, "suspended_until": suspended_until, "message": message}


def _open_deletion_request(supabase, user_id: str, username: str | None) -> None:
    try:
        pending = (
            supabase.table("deletion_requests").select("id").eq("user_id", user_id).eq("status", "pending").limit(1).execute().data
        )
        if not pending:
            supabase.table("deletion_requests").insert(
                {"user_id": user_id, "username": username, "status": "pending"}
            ).execute()
    except Exception:
        logger.exception("Failed to open deletion request for %s", user_id)


# ─── Admin actions ──────────────────────────────────────────────────────────
class ModerationError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


def _require_warnings(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_RESTORE_WARNINGS:
        raise ModerationError(f"warnings must be a whole number from 0 to {MAX_RESTORE_WARNINGS}")
    return value


def _require_user(supabase, user_id: str) -> dict:
    rows = supabase.table("profiles").select(STATE_COLUMNS).eq("id", user_id).limit(1).execute().data
    if not rows:
        raise ModerationError("User not found", 404)
    return rows[0]


def lift_suspension(supabase, admin_id: str, user_id: str, warnings: int | None = None) -> None:
    """Ends a suspension early. The warning count stays where it is unless the
    admin passes `warnings` (0-2)."""
    user = _require_user(supabase, user_id)
    if user.get("moderation_status") != "suspended":
        raise ModerationError("That account isn't suspended", 409)
    update: dict = {"moderation_status": "active", "suspended_until": None}
    if warnings is not None:
        update["warning_count"] = _require_warnings(warnings)
    supabase.table("profiles").update(update).eq("id", user_id).execute()
    _log_event(supabase, user_id=user_id, username=user.get("username"), action="suspension_lifted", admin_id=admin_id)


def set_warnings(supabase, admin_id: str, user_id: str, warnings: int) -> None:
    """Sets the warning count of an account that is not blocked (blocked
    accounts are handled through their deletion request)."""
    warnings = _require_warnings(warnings)
    user = _require_user(supabase, user_id)
    if user.get("moderation_status") == "blocked":
        raise ModerationError("That account is blocked - resolve its deletion request instead", 409)
    supabase.table("profiles").update({"warning_count": warnings}).eq("id", user_id).execute()
    _log_event(
        supabase, user_id=user_id, username=user.get("username"), action="warnings_set",
        admin_id=admin_id, note=f"{user.get('warning_count') or 0} -> {warnings}",
    )


def _pending_request(supabase, request_id: str) -> dict:
    rows = supabase.table("deletion_requests").select("*").eq("id", request_id).limit(1).execute().data
    if not rows:
        raise ModerationError("Deletion request not found", 404)
    if rows[0].get("status") != "pending":
        raise ModerationError("That request has already been resolved", 409)
    return rows[0]


def reject_deletion(supabase, admin_id: str, request_id: str, warnings: int) -> None:
    """Hands the account back, with 0, 1 or 2 warnings of the admin's choosing."""
    warnings = _require_warnings(warnings)
    req = _pending_request(supabase, request_id)
    supabase.table("profiles").update(
        {"warning_count": warnings, "moderation_status": "active", "suspended_until": None}
    ).eq("id", req["user_id"]).execute()
    supabase.table("deletion_requests").update(
        {"status": "rejected", "resolved_at": _now().isoformat(), "resolved_by": admin_id, "restored_warnings": warnings}
    ).eq("id", request_id).execute()
    _log_event(
        supabase, user_id=req["user_id"], username=req.get("username"), action="restored",
        admin_id=admin_id, note=f"restored with {warnings} warning(s)",
    )


def approve_deletion(supabase, admin_id: str, request_id: str) -> None:
    """Deletes the account (all its data cascades via the database)."""
    req = _pending_request(supabase, request_id)
    _delete_auth_user(req["user_id"])
    supabase.table("deletion_requests").update(
        {"status": "approved", "resolved_at": _now().isoformat(), "resolved_by": admin_id}
    ).eq("id", request_id).execute()
    _log_event(supabase, user_id=req["user_id"], username=req.get("username"), action="deleted", admin_id=admin_id)


def _delete_auth_user(user_id: str) -> None:
    """Same Admin API call the self-service account deletion uses
    (controllers/profile.py): removing auth.users cascades to profiles and
    everything hanging off it."""
    resp = http_requests.delete(
        f"{current_app.config['SUPABASE_URL']}/auth/v1/admin/users/{user_id}",
        headers={
            "apikey": current_app.config["SUPABASE_SERVICE_ROLE_KEY"],
            "Authorization": f"Bearer {current_app.config['SUPABASE_SERVICE_ROLE_KEY']}",
        },
        timeout=10,
    )
    if resp.status_code not in (200, 204, 404):
        raise ModerationError("Supabase refused to delete the account", 502)


# ─── Admin reads ────────────────────────────────────────────────────────────
def overview(supabase) -> dict:
    pending = (
        supabase.table("deletion_requests").select("*").eq("status", "pending").order("created_at").execute().data
    )
    user_ids = [r["user_id"] for r in pending]
    profiles = {}
    events_by_user: dict[str, list[dict]] = {}
    if user_ids:
        for p in supabase.table("profiles").select(STATE_COLUMNS).in_("id", user_ids).execute().data:
            profiles[p["id"]] = p
        for e in (
            supabase.table("moderation_events").select("*").in_("user_id", user_ids).order("created_at", desc=True).execute().data
        ):
            events_by_user.setdefault(e["user_id"], []).append(e)
    for r in pending:
        r["profile"] = profiles.get(r["user_id"])
        r["events"] = events_by_user.get(r["user_id"], [])[:10]

    suspended = (
        supabase.table("profiles").select(STATE_COLUMNS).eq("moderation_status", "suspended").order("suspended_until").execute().data
    )
    recent = supabase.table("moderation_events").select("*").order("created_at", desc=True).limit(50).execute().data
    return {"pending_deletions": pending, "suspended": suspended, "recent_events": recent}
