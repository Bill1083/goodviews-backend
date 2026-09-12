import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from flask import current_app

from app.services.supabase_client import get_supabase


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


def create_trusted_device(user_id: str) -> tuple[str, str]:
    """Issues a new trusted-device token for user_id and stores only its hash
    (mirrors how a leaked DB shouldn't hand out usable credentials). Returns
    (raw_token, expires_at_iso) — the raw token is only ever shown once, here,
    and the caller is responsible for persisting it client-side."""
    raw_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(days=current_app.config["TRUSTED_DEVICE_TTL_DAYS"])

    get_supabase().table("trusted_devices").insert(
        {
            "user_id": user_id,
            "token_hash": _hash_token(raw_token),
            "expires_at": expires_at.isoformat(),
        }
    ).execute()

    return raw_token, expires_at.isoformat()


def verify_trusted_device(user_id: str, raw_token: str) -> bool:
    """Checks whether raw_token is a live, unexpired trusted-device token
    belonging to user_id, bumping last_used_at on success."""
    if not raw_token:
        return False

    supabase = get_supabase()
    result = (
        supabase.table("trusted_devices")
        .select("id, expires_at")
        .eq("user_id", user_id)
        .eq("token_hash", _hash_token(raw_token))
        .limit(1)
        .execute()
    )
    if not result.data:
        return False

    row = result.data[0]
    expires_at = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
    if expires_at < datetime.now(timezone.utc):
        # Expired — clean it up so it isn't retried and doesn't sit around forever.
        supabase.table("trusted_devices").delete().eq("id", row["id"]).execute()
        return False

    supabase.table("trusted_devices").update(
        {"last_used_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", row["id"]).execute()
    return True
