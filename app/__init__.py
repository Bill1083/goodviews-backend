import click
from flask import Flask
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix

from app.config import Config

limiter = Limiter(key_func=get_remote_address)


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)

    # nginx sits in front of every request (see the staging/prod nginx confs),
    # so request.remote_addr is otherwise always nginx's own address — which
    # made every rate limit a single bucket shared by every visitor to the
    # site, not per-visitor. Trust exactly one proxy hop's X-Forwarded-For/
    # X-Forwarded-Proto so get_remote_address (used by @limiter.limit
    # everywhere) sees the real client IP.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

    # CORS — only allow configured origins. allow_headers must be listed
    # explicitly (rather than relying on flask-cors' default) since prod
    # serves the API from a separate subdomain (api.goodviews.online) from
    # the frontend (goodviews.online) — a genuinely cross-origin request,
    # unlike staging where nginx proxies both under one origin. Without an
    # explicit allow_headers, the browser's CORS preflight rejected the
    # custom X-Trusted-Device-Token header (sent on every request once a
    # device is remembered — see apiClient.ts), breaking every API call for
    # anyone with a remembered device, not just the trusted-device ones.
    CORS(
        app,
        resources={r"/api/*": {"origins": app.config["CORS_ORIGINS"]}},
        allow_headers=["Authorization", "Content-Type", "X-Trusted-Device-Token"],
    )

    # Rate limiter — backed by Redis when available
    limiter.init_app(app)

    # Register blueprints
    from app.controllers.movies import movies_bp
    from app.controllers.reviews import reviews_bp
    from app.controllers.categories import categories_bp
    from app.controllers.friends import friends_bp
    from app.controllers.friend_groups import friend_groups_bp
    from app.controllers.profile import profile_bp
    from app.controllers.watchlist import watchlist_bp
    from app.controllers.notifications import notifications_bp
    from app.controllers.people import people_bp
    from app.controllers.favourites import favourites_bp
    from app.controllers.auth import auth_bp
    from app.controllers.onboarding import onboarding_bp
    from app.controllers.stats import stats_bp

    app.register_blueprint(movies_bp, url_prefix="/api/movies")
    app.register_blueprint(reviews_bp, url_prefix="/api/reviews")
    app.register_blueprint(categories_bp, url_prefix="/api/categories")
    app.register_blueprint(friends_bp, url_prefix="/api/friends")
    app.register_blueprint(friend_groups_bp, url_prefix="/api/groups")
    app.register_blueprint(profile_bp, url_prefix="/api/profile")
    app.register_blueprint(watchlist_bp, url_prefix="/api/watchlist")
    app.register_blueprint(notifications_bp, url_prefix="/api/notifications")
    app.register_blueprint(people_bp, url_prefix="/api/people")
    app.register_blueprint(favourites_bp, url_prefix="/api/favourites")
    app.register_blueprint(auth_bp, url_prefix="/api/auth")
    app.register_blueprint(onboarding_bp, url_prefix="/api/onboarding")
    app.register_blueprint(stats_bp, url_prefix="/api/stats")

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.cli.command("prune-movies")
    def prune_movies_command():
        """Delete movie rows not viewed in MOVIE_PRUNE_AFTER_DAYS days that
        aren't referenced by any watchlist/review/notification. Intended to be
        run on a schedule (e.g. a Render Cron Job or system cron) — not called
        automatically by the app itself."""
        from app.services import movie_cache

        deleted = movie_cache.prune_unwatched_movies()
        print(f"Pruned {deleted} movie(s).")

    @app.cli.command("refresh-stale-movies")
    def refresh_stale_movies_command():
        """Force-refreshes any movie row TMDB data older than
        MOVIE_MAX_CACHE_AGE_DAYS (default 150) from TMDB — required for
        compliance with TMDB's API Terms of Use, which prohibit caching their
        data for longer than 6 months. Run on the same kind of schedule as
        prune-movies (e.g. a Render Cron Job or system cron), at least monthly."""
        from app.services import movie_cache

        refreshed = movie_cache.refresh_stale_movies()
        print(f"Refreshed {refreshed} stale movie(s).")

    @app.cli.command("backfill-movie-extras")
    @click.option("--limit", default=0, type=int, help="Refresh at most this many movies (0 = all).")
    def backfill_movie_extras_command(limit: int):
        """One-off after applying sql/008: fills the stats columns (directors,
        top_cast, language, countries, budget, ...) for every movie referenced
        by a review or watchlist entry by re-fetching it from TMDB. Safe to
        re-run - only rows still missing the columns are touched."""
        from app.services import movie_cache

        refreshed = movie_cache.backfill_movie_extras(limit or None)
        print(f"Backfilled extras for {refreshed} movie(s).")

    return app
