import logging

from flask import Blueprint, jsonify, request

from app import limiter
from app.utils.auth import require_auth
from app.utils.sanitize import sanitize_text
from app.utils.social import filter_friend_ids, filter_owned_group_ids
from app.services import tmdb
from app.services import movie_cache
from app.services import daily_picks, recommendations, streaming_picks, streaming_worlds
from app.services.supabase_client import get_supabase
from app.utils.errors import server_error
from app.utils.tz import parse_tz

logger = logging.getLogger(__name__)

movies_bp = Blueprint("movies", __name__)


@movies_bp.get("/search")
@limiter.limit("60 per minute")
def search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "Query parameter 'q' is required"}), 400
    if len(query) > 200:
        return jsonify({"error": "Query too long"}), 400

    page = request.args.get("page", 1, type=int)
    page = max(1, min(page, 500))  # TMDB supports up to page 500

    try:
        data = tmdb.search_movies(query, page)
        return jsonify(data)
    except Exception as exc:
        return server_error("Failed to fetch movies", exc, 502)


@movies_bp.get("/trending")
@limiter.limit("60 per minute")
def trending():
    """Most popular / most talked-about movies this week."""
    page = request.args.get("page", 1, type=int)
    page = max(1, min(page, 500))

    try:
        data = tmdb.get_trending_movies(page)
        return jsonify(data)
    except Exception as exc:
        return server_error("Failed to fetch trending movies", exc, 502)


@movies_bp.get("/top-rated")
@limiter.limit("60 per minute")
def top_rated():
    """All-time top-rated movies."""
    page = request.args.get("page", 1, type=int)
    page = max(1, min(page, 500))

    try:
        data = tmdb.get_top_rated_movies(page)
        return jsonify(data)
    except Exception as exc:
        return server_error("Failed to fetch top rated movies", exc, 502)


def _streaming_filter_prefs(supabase, user_id: str) -> list[int]:
    """The provider ids to filter by, or [] if the filter is off (or not yet
    migrated) — [] is what streaming_picks.filter_by_availability treats as
    a no-op, so callers can apply it unconditionally."""
    try:
        row = (
            supabase.table("profiles")
            .select("streaming_filter_enabled, streaming_provider_ids")
            .eq("id", user_id)
            .single()
            .execute()
        ).data or {}
    except Exception as exc:
        # sql/012 or sql/013 not applied yet — same as the filter being off.
        if "streaming_filter_enabled" not in str(exc) and "streaming_provider_ids" not in str(exc):
            logger.exception("Failed to read streaming-filter prefs for %s", user_id)
        return []
    if not row.get("streaming_filter_enabled"):
        return []
    return row.get("streaming_provider_ids") or []


@movies_bp.get("/for-you")
@require_auth
@limiter.limit("10 per minute")
def for_you():
    """Personalized recommendation feed — see app.services.recommendations.
    ?force=true bypasses the 24h cache and recomputes immediately. Filtered
    to the user's streaming services if they've turned that on in Settings,
    backfilled so the filter changes *which* films show, not how many
    (never filtered: Most Popular This Week — see streaming_picks.py)."""
    user = request.current_user
    supabase = get_supabase()
    force = request.args.get("force", "").lower() in ("true", "1")
    try:
        provider_ids = _streaming_filter_prefs(supabase, str(user.id))
        data = recommendations.get_recommendations_for_user_streaming(str(user.id), provider_ids, force=force)
        return jsonify(data)
    except Exception as exc:
        return server_error("Failed to fetch recommendations", exc, 500)


@movies_bp.get("/movies-of-the-day")
@require_auth
@limiter.limit("20 per minute")
def movies_of_the_day():
    """Three picks, new every day in the caller's timezone (?tz=, IANA) —
    see app.services.daily_picks. ?force=true recomputes today's. Filtered
    (and backfilled back up to three) the same way as for_you."""
    user = request.current_user
    supabase = get_supabase()
    tz = parse_tz(request.args.get("tz"))
    if tz is None:
        return jsonify({"error": "Invalid tz"}), 400
    force = request.args.get("force", "").lower() in ("true", "1")
    try:
        provider_ids = _streaming_filter_prefs(supabase, str(user.id))
        data = daily_picks.get_daily_picks_streaming(str(user.id), tz, provider_ids, force=force)
        return jsonify(data)
    except Exception as exc:
        return server_error("Failed to fetch movies of the day", exc, 500)


@movies_bp.get("/picks-of-the-week")
@require_auth
@limiter.limit("20 per minute")
def picks_of_the_week():
    """The old name, kept so a browser still running the pre-rename client
    keeps working through a deploy. Serves the same daily picks, on UTC days."""
    user = request.current_user
    supabase = get_supabase()
    try:
        provider_ids = _streaming_filter_prefs(supabase, str(user.id))
        data = daily_picks.get_daily_picks_streaming(str(user.id), parse_tz("UTC"), provider_ids)
        return jsonify(data)
    except Exception as exc:
        return server_error("Failed to fetch movies of the day", exc, 500)


@movies_bp.get("/streaming-providers")
@limiter.limit("30 per minute")
def streaming_providers():
    """The curated streaming-provider picker list for Settings — not
    user-specific, so no auth needed."""
    try:
        return jsonify(streaming_picks.list_streaming_providers())
    except Exception as exc:
        return server_error("Failed to fetch streaming providers", exc, 502)


@movies_bp.get("/streaming-worlds/<int:provider_id>")
@require_auth
@limiter.limit("30 per minute")
def streaming_world(provider_id: int):
    """One streaming service's own Discover sub-page — Popular/For You/
    Different, all scoped to just that service. See
    app.services.streaming_worlds."""
    if not (0 < provider_id < 100000):
        return jsonify({"error": "Invalid provider_id"}), 400
    user = request.current_user
    try:
        data = streaming_worlds.get_streaming_world(str(user.id), provider_id)
        return jsonify(data)
    except Exception as exc:
        return server_error("Failed to fetch streaming world", exc, 500)


@movies_bp.post("/not-interested")
@require_auth
@limiter.limit("30 per minute")
def not_interested():
    """Dismiss a recommendation: splice a replacement into For You without
    waiting for the 24h refresh, and take it out of today's Movies of the
    Day (the next fetch refills that slot)."""
    user = request.current_user
    body = request.get_json(silent=True) or {}
    movie_id = body.get("movie_id")
    if not movie_id or not isinstance(movie_id, int):
        return jsonify({"error": "Valid movie_id (integer) is required"}), 400
    scope = body.get("scope", "movie")
    if scope not in ("movie", "type"):
        return jsonify({"error": "scope must be 'movie' or 'type'"}), 400
    try:
        replacement = recommendations.mark_not_interested(str(user.id), movie_id, scope)
    except Exception as exc:
        return server_error("Failed to mark as not interested", exc, 500)
    try:
        daily_dropped = daily_picks.drop_from_daily_picks(get_supabase(), str(user.id), movie_id)
    except Exception:
        # The dismissal itself is recorded; the daily picks exclude it from
        # every future plan regardless.
        logger.exception("Failed to drop a dismissed film from Movies of the Day")
        daily_dropped = False
    return jsonify({"replacement": replacement, "daily_pick_dropped": daily_dropped})


@movies_bp.get("/<int:movie_id>")
@limiter.limit("60 per minute")
def details(movie_id: int):
    try:
        data = movie_cache.get_movie(movie_id)
        return jsonify(data)
    except Exception as exc:
        return server_error("Failed to fetch movie details", exc, 502)


@movies_bp.get("/<int:movie_id>/images")
@limiter.limit("60 per minute")
def images(movie_id: int):
    try:
        data = movie_cache.get_movie_images(movie_id)
        return jsonify(data)
    except Exception as exc:
        return server_error("Failed to fetch movie images", exc, 502)


@movies_bp.get("/<int:movie_id>/collection")
@limiter.limit("60 per minute")
def collection(movie_id: int):
    """The movie's franchise, if any — other sequels/prequels/spin-offs to
    show as a "<Franchise> Universe" row on the detail modal. See
    app.services.movie_cache.get_movie_collection."""
    try:
        data = movie_cache.get_movie_collection(movie_id)
        return jsonify(data)
    except Exception as exc:
        return server_error("Failed to fetch movie collection", exc, 502)


@movies_bp.post("/<int:movie_id>/refresh")
@require_auth
@limiter.limit("5 per hour")
def refresh_movie(movie_id: int):
    """User-triggered force refresh: bypasses all TTL/cache checks and
    overwrites the stored record with fresh TMDB data."""
    try:
        data = movie_cache.force_refresh_movie(movie_id)
        return jsonify(data)
    except Exception as exc:
        return server_error("Failed to refresh movie", exc, 502)


@movies_bp.post("/recommend")
@require_auth
@limiter.limit("20 per hour")
def recommend_movie():
    """Send a movie recommendation to individual friends and/or friend groups."""
    user = request.current_user
    body = request.get_json(silent=True) or {}

    movie_id = body.get("movie_id")
    if not movie_id or not isinstance(movie_id, int):
        return jsonify({"error": "Valid movie_id (integer) is required"}), 400

    raw_friend_ids = body.get("friend_ids") or []
    friend_ids = [str(f) for f in raw_friend_ids if f] if isinstance(raw_friend_ids, list) else []
    raw_group_ids = body.get("group_ids") or []
    group_ids = [str(g) for g in raw_group_ids if g] if isinstance(raw_group_ids, list) else []

    movie_data = {
        "id": movie_id,
        "title": sanitize_text(body.get("title", "")),
        "poster_path": body.get("poster_path"),
        "release_date": body.get("release_date"),
    }

    supabase = get_supabase()
    try:
        supabase.table("movies").upsert(movie_data, on_conflict="id").execute()

        # Only fan out to friends the caller actually has, and groups they actually
        # own — otherwise any logged-in user could spam arbitrary users/probe group
        # sizes by passing IDs they found or guessed.
        recipient_ids: set[str] = filter_friend_ids(supabase, str(user.id), friend_ids)
        allowed_group_ids = filter_owned_group_ids(supabase, str(user.id), group_ids)

        # Expand groups to member user_ids
        for gid in allowed_group_ids:
            members = (
                supabase.table("group_members")
                .select("user_id")
                .eq("group_id", gid)
                .execute()
            )
            for m in members.data:
                recipient_ids.add(m["user_id"])

        # Don't notify yourself
        recipient_ids.discard(str(user.id))

        if recipient_ids:
            # Skip anyone who already has an un-dismissed recommendation of
            # this exact movie from this exact sender — a double-tap (common
            # on mobile) re-firing this request shouldn't leave two identical
            # notifications sitting in someone's feed.
            existing = (
                supabase.table("notifications")
                .select("user_id")
                .eq("sender_id", str(user.id))
                .eq("movie_id", movie_id)
                .eq("dismissed", False)
                .in_("user_id", list(recipient_ids))
                .execute()
            )
            already_notified = {r["user_id"] for r in existing.data}
            recipient_ids -= already_notified

        if recipient_ids:
            notif_rows = [
                {
                    "user_id": rid,
                    "sender_id": str(user.id),
                    "movie_id": movie_id,
                    "message": "recommended a movie to you",
                }
                for rid in recipient_ids
            ]
            supabase.table("notifications").insert(notif_rows).execute()

        return jsonify({"message": "Sent", "recipient_count": len(recipient_ids)}), 200
    except Exception as exc:
        return server_error("Failed to send recommendation", exc, 500)
