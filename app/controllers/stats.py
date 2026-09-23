"""Taste dashboard + Wrapped endpoints. All of the maths lives in
app/services/stats.py; this layer does auth, timezone parsing, the Wrapped
unlock gate and Redis caching."""
import logging
from collections import Counter
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Blueprint, current_app, jsonify, request

from app import limiter
from app.services import stats
from app.services.cache import cache_get, cache_set, stats_version
from app.services.supabase_client import get_supabase
from app.utils.auth import require_auth
from app.utils.errors import server_error
from app.utils.social import load_viewable_profile

logger = logging.getLogger(__name__)

stats_bp = Blueprint("stats", __name__)

# Any year before this has no film logs — the app didn't exist. Keeps the
# per-year route from being probed with nonsense.
EARLIEST_YEAR = 2020


def _now() -> datetime:
    """Server clock, in one place so tests can pin it."""
    return datetime.now(timezone.utc)


def _parse_tz() -> ZoneInfo | None:
    """The caller's IANA timezone (?tz=Australia/Sydney), used only to bucket
    years / months / streaks in their local time. The Wrapped unlock check
    never uses it (see stats.unlock_status)."""
    raw = (request.args.get("tz") or "UTC").strip()
    if not raw or len(raw) > 64:
        return None
    try:
        return ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _cache_key(kind: str, user_id: str, tz: ZoneInfo, suffix: str = "") -> str:
    # The trailing version segment is bumped by cache.invalidate_user_stats
    # on every write of the user's own data.
    return f"stats:{kind}:{user_id}:{tz.key}{suffix}:v{stats_version(user_id)}"


def _unlock(year: int, now: datetime) -> tuple[str, datetime | None]:
    cfg = current_app.config
    return stats.unlock_status(
        year,
        now=now,
        unlock_month_day=cfg["WRAPPED_UNLOCK_MONTH_DAY"],
        preview=bool(cfg.get("WRAPPED_PREVIEW_UNLOCK")),
    )


def _dashboard(user_id: str, tz: ZoneInfo):
    """Whose stats these are is the only difference between the two routes —
    the payload, the cache key and the TTL are identical, so a friend viewing
    your profile reads the very same cached copy you do."""
    key = _cache_key("dashboard", user_id, tz)
    cached = cache_get(key)
    if cached:
        return jsonify(cached)

    # No friend section on the dashboard (that comparison is a Wrapped
    # reveal), so skip the friend queries — they are the slow half.
    data = stats.load_taste_data(user_id, get_supabase(), include_social=False)
    payload = stats.compute_dashboard(data, now=_now(), tz=tz)
    cache_set(key, payload, ttl=current_app.config["STATS_DASHBOARD_TTL_SECONDS"])
    return jsonify(payload)


@stats_bp.get("/me")
@require_auth
@limiter.limit("30 per minute")
def my_stats():
    """All-time taste dashboard for the caller."""
    user = request.current_user
    tz = _parse_tz()
    if tz is None:
        return jsonify({"error": "Invalid tz"}), 400
    try:
        return _dashboard(str(user.id), tz)
    except Exception as exc:
        return server_error("Failed to compute stats", exc, 500)


@stats_bp.get("/user/<user_id>")
@require_auth
@limiter.limit("30 per minute")
def user_stats(user_id: str):
    """Someone else's taste card, behind the same gate as their profile —
    the Wrapped is not reachable this way at any time of year."""
    viewer = request.current_user
    tz = _parse_tz()
    if tz is None:
        return jsonify({"error": "Invalid tz"}), 400
    try:
        _, outcome = load_viewable_profile(get_supabase(), str(viewer.id), user_id)
        if outcome == "not_found":
            return jsonify({"error": "Profile not found"}), 404
        if outcome == "private":
            return jsonify({"error": "private"}), 403
        return _dashboard(str(user_id), tz)
    except Exception as exc:
        return server_error("Failed to compute stats", exc, 500)


@stats_bp.get("/wrapped")
@require_auth
@limiter.limit("30 per minute")
def wrapped_availability():
    """What the profile may show about the Wrapped.

    `current` is the year in its reveal window — non-null only between the
    unlock date and the end of that year, so before December the profile has
    nothing to announce and no countdown to give the game away. `history` is
    every earlier year that produced a Wrapped, available all year round."""
    user = request.current_user
    tz = _parse_tz()
    if tz is None:
        return jsonify({"error": "Invalid tz"}), 400
    now = _now()

    try:
        dates = stats.load_review_dates(str(user.id), get_supabase())
    except Exception as exc:
        return server_error("Failed to load Wrapped availability", exc, 500)

    per_year = Counter(stats.wrapped_years([dt], tz)[0] for dt in dates)
    min_films = current_app.config["WRAPPED_MIN_FILMS"]

    current = None
    if _unlock(now.year, now)[0] == "ready":
        films = per_year.get(now.year, 0)
        current = {
            "year": now.year,
            "status": "ready" if films >= min_films else "not_enough",
            "films": films,
            "min_films": min_films,
        }

    history = [
        {"year": year, "films": films}
        for year, films in sorted(per_year.items(), reverse=True)
        # A past year below the threshold can never reach it (the year is
        # over), so it is simply not part of the history.
        if EARLIEST_YEAR <= year < now.year and films >= min_films
    ]

    return jsonify({
        "current_year": now.year,
        "server_time": now.isoformat(),
        "current": current,
        "history": history,
    })


@stats_bp.get("/wrapped/<int:year>")
@require_auth
@limiter.limit("20 per minute")
def wrapped_year(year: int):
    """One year's Wrapped. 423 while locked — checked before any data is
    loaded, so nothing about the year exists in a reachable form early."""
    user = request.current_user
    tz = _parse_tz()
    if tz is None:
        return jsonify({"error": "Invalid tz"}), 400
    now = _now()

    if year < EARLIEST_YEAR or year > now.year:
        return jsonify({"error": "No Wrapped for that year", "year": year}), 404

    status, unlocks_at = _unlock(year, now)
    if status == "locked":
        return jsonify({"error": "locked", "year": year, "unlocks_at": unlocks_at.isoformat()}), 423

    user_id = str(user.id)
    key = _cache_key("wrapped", user_id, tz, f":{year}")
    cached = cache_get(key)
    if cached:
        return jsonify(cached)

    try:
        data = stats.load_taste_data(user_id, get_supabase())
        payload = stats.compute_wrapped(
            data, year, now=now, tz=tz, min_films=current_app.config["WRAPPED_MIN_FILMS"],
        )
    except Exception as exc:
        return server_error("Failed to compute Wrapped", exc, 500)

    if payload["status"] == "not_enough" and payload["films"] == 0:
        return jsonify({"error": "No activity in that year", "year": year}), 404

    # The current year keeps changing through December; past years are settled.
    ttl_key = "STATS_WRAPPED_TTL_SECONDS" if year < now.year else "STATS_DASHBOARD_TTL_SECONDS"
    cache_set(key, payload, ttl=current_app.config[ttl_key])
    return jsonify(payload)
