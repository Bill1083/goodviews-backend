import os


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-change-in-production")
    TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")
    TMDB_BASE_URL = os.getenv("TMDB_BASE_URL", "https://api.themoviedb.org/3")
    SUPABASE_URL = os.getenv("SUPABASE_URL", "")
    SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

    # TMDB search cache — transient only, never persisted to the DB (1-4h range)
    SEARCH_CACHE_TTL_SECONDS = int(os.getenv("SEARCH_CACHE_TTL_SECONDS", 60 * 60 * 2))

    # Segmented movie-details cache TTLs, checked as Postgres timestamps
    MOVIE_CORE_TTL_DAYS = int(os.getenv("MOVIE_CORE_TTL_DAYS", 60))              # plot/runtime/cast: 30-90d
    MOVIE_MEDIA_TTL_DAYS = int(os.getenv("MOVIE_MEDIA_TTL_DAYS", 10))            # posters/trailers: 7-14d
    MOVIE_PROVIDERS_TTL_HOURS = int(os.getenv("MOVIE_PROVIDERS_TTL_HOURS", 24))  # watch providers: 24-48h

    # Prune movies not viewed in this many days (and not referenced by any
    # watchlist/review/notification) to keep the DB from growing forever.
    MOVIE_PRUNE_AFTER_DAYS = int(os.getenv("MOVIE_PRUNE_AFTER_DAYS", 90))

    # TMDB's API Terms of Use (Section 1.C) prohibit caching TMDB-sourced
    # information for longer than 6 months. A movie referenced by a review or
    # watchlist item is never pruned, so without this it could sit unrefreshed
    # indefinitely if nobody ever revisits it. Set comfortably under 6 months
    # (~182 days) so a job that only runs monthly still stays compliant.
    MOVIE_MAX_CACHE_AGE_DAYS = int(os.getenv("MOVIE_MAX_CACHE_AGE_DAYS", 150))
    CORS_ORIGINS = [
        origin.strip()
        for origin in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",")
    ]

    # "Remember this device" MFA opt-out window
    TRUSTED_DEVICE_TTL_DAYS = int(os.getenv("TRUSTED_DEVICE_TTL_DAYS", 30))

    # ─── Taste stats + Wrapped ────────────────────────────────────────────
    # Month-day (MM-DD, evaluated in UTC) on which the current year's Wrapped
    # becomes viewable. Past years are always viewable.
    WRAPPED_UNLOCK_MONTH_DAY = os.getenv("WRAPPED_UNLOCK_MONTH_DAY", "12-01")
    # Local development only: bypasses the unlock date so the current year's
    # Wrapped can be played at any time. Never set this in production.
    WRAPPED_PREVIEW_UNLOCK = os.getenv("WRAPPED_PREVIEW_UNLOCK", "0").strip().lower() in ("1", "true", "yes")
    # Fewer non-onboarding ratings than this in a year -> "not enough" screen.
    WRAPPED_MIN_FILMS = int(os.getenv("WRAPPED_MIN_FILMS", 5))
    # Redis TTLs for computed dashboard / Wrapped payloads. The user's own
    # writes bump a per-user cache version (app/services/cache.py), so these
    # only bound how long a payload can outlive a *friend's* activity.
    STATS_DASHBOARD_TTL_SECONDS = int(os.getenv("STATS_DASHBOARD_TTL_SECONDS", 60 * 60))
    STATS_WRAPPED_TTL_SECONDS = int(os.getenv("STATS_WRAPPED_TTL_SECONDS", 60 * 60 * 24))
    # Rate limiting
    RATELIMIT_DEFAULT = "200 per day;50 per hour"
    RATELIMIT_STORAGE_URI = os.getenv("REDIS_URL", "memory://")
    RATELIMIT_SWALLOW_ERRORS = True
