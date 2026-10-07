import logging

from flask import Blueprint, jsonify, request

from app import limiter
from app.utils.auth import require_auth
from app.utils.sanitize import sanitize_text
from app.utils.social import filter_friend_ids, filter_owned_group_ids
from app.services import tmdb
from app.services import movie_cache
from app.services import daily_picks, recommendations, streaming_picks
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


@movies_bp.get("/for-you")
@require_auth
@limiter.limit("10 per minute")
def for_you():
    """Personalized recommendation feed — see app.services.recommendations.
    ?force=true bypasses the 24h cache and recomputes immediately."""
    user = request.current_user
    force = request.args.get("force", "").lower() in ("true", "1")
    try:
        data = recommendations.get_recommendations_for_user(str(user.id), force=force)
        return jsonify(data)
    except Exception as exc:
        return server_error("Failed to fetch recommendations", exc, 500)


@movies_bp.get("/movies-of-the-day")
@require_auth
@limiter.limit("20 per minute")
def movies_of_the_day():
    """Three picks, new every day in the caller's timezone (?tz=, IANA) —
    see app.services.daily_picks. ?force=true recomputes today's."""
    user = request.current_user
    tz = parse_tz(request.args.get("tz"))
    if tz is None:
        return jsonify({"error": "Invalid tz"}), 400
    force = request.args.get("force", "").lower() in ("true", "1")
    try:
        return jsonify(daily_picks.get_daily_picks(str(user.id), tz, force=force))
    except Exception as exc:
        return server_error("Failed to fetch movies of the day", exc, 500)


@movies_bp.get("/picks-of-the-week")
@require_auth
@limiter.limit("20 per minute")
def picks_of_the_week():
    """The old name, kept so a browser still running the pre-rename client
    keeps working through a deploy. Serves the same daily picks, on UTC days."""
    user = request.current_user
    try:
        return jsonify(daily_picks.get_daily_picks(str(user.id), parse_tz("UTC")))
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


@movies_bp.get("/streaming-picks")
@require_auth
@limiter.limit("20 per minute")
def streaming_picks_route():
    """"Your Streaming Services": popular/well-rated/genre-affinity films
    available on the providers this user selected in Settings.
    ?exclude_seen=true drops anything they've already reviewed."""
    user = request.current_user
    supabase = get_supabase()
    exclude_seen = request.args.get("exclude_seen", "").lower() in ("true", "1")

    try:
        profile_result = (
            supabase.table("profiles")
            .select("streaming_provider_ids, onboarding_genre_ids")
            .eq("id", str(user.id))
            .single()
            .execute()
        )
        row = profile_result.data or {}
    except Exception as exc:
        # sql/012 not applied yet — no provider ids to read, so there's
        # nothing to show rather than a hard failure.
        if "streaming_provider_ids" not in str(exc):
            return server_error("Failed to fetch streaming picks", exc, 500)
        row = {}

    provider_ids = row.get("streaming_provider_ids") or []
    genre_ids = row.get("onboarding_genre_ids") or []

    try:
        exclude_ids: set[int] = set()
        if exclude_seen and provider_ids:
            reviewed = (
                supabase.table("reviews")
                .select("movie_id")
                .eq("user_id", str(user.id))
                .execute()
            )
            exclude_ids = {r["movie_id"] for r in reviewed.data}
        results = streaming_picks.get_streaming_picks(provider_ids, genre_ids, exclude_ids)
        return jsonify({"results": results})
    except Exception as exc:
        return server_error("Failed to fetch streaming picks", exc, 500)


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
