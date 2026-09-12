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
    CORS_ORIGINS = [
        origin.strip()
        for origin in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",")
    ]

    # "Remember this device" MFA opt-out window
    TRUSTED_DEVICE_TTL_DAYS = int(os.getenv("TRUSTED_DEVICE_TTL_DAYS", 30))
    # Rate limiting
    RATELIMIT_DEFAULT = "200 per day;50 per hour"
    RATELIMIT_STORAGE_URI = os.getenv("REDIS_URL", "memory://")
    RATELIMIT_SWALLOW_ERRORS = True
