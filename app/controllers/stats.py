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


@stats_bp.get("/me")
@require_auth
@limiter.limit("30 per minute")
def my_stats():
    """All-time taste dashboard for the caller."""
    user = request.current_user
    tz = _parse_tz()
    if tz is None:
        return jsonify({"error": "Invalid tz"}), 400
    user_id = str(user.id)

    key = _cache_key("dashboard", user_id, tz)
    cached = cache_get(key)
    if cached:
        return jsonify(cached)

    try:
        data = stats.load_taste_data(user_id, get_supabase())
        payload = stats.compute_dashboard(data, now=_now(), tz=tz)
    except Exception as exc:
        return server_error("Failed to compute stats", exc, 500)

    cache_set(key, payload, ttl=current_app.config["STATS_DASHBOARD_TTL_SECONDS"])
    return jsonify(payload)


@stats_bp.get("/wrapped")
@require_auth
@limiter.limit("30 per minute")
def wrapped_availability():
    """Which years have a Wrapped and whether each is viewable yet. A locked
    year exposes nothing but its unlock time — not even a film count."""
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
    years = set(per_year) | {now.year}
    min_films = current_app.config["WRAPPED_MIN_FILMS"]

    out = []
    for year in sorted(years, reverse=True):
        status, unlocks_at = _unlock(year, now)
        if status == "future" or year < EARLIEST_YEAR:
            continue
        if status == "locked":
            out.append({"year": year, "status": "locked", "unlocks_at": unlocks_at.isoformat()})
            continue
        films = per_year.get(year, 0)
        out.append({
            "year": year,
            "status": "ready" if films >= min_films else "not_enough",
            "films": films,
            "min_films": min_films,
        })
    return jsonify({"current_year": now.year, "server_time": now.isoformat(), "years": out})


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
