from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Blueprint, jsonify

from app import limiter
from app.services import movie_cache
from app.utils.auth import require_auth
from app.utils.errors import server_error

onboarding_bp = Blueprint("onboarding", __name__)

# Hand-picked, well-known, genre-spanning movies for the "quick ratings"
# onboarding step. TMDB ids — any that fail to resolve (renumbered/removed
# on TMDB) are silently skipped rather than failing the whole list, so this
# doesn't need to be perfectly maintained.
CURATED_MOVIE_IDS = [
    278, 238, 155, 680, 13, 27205, 603, 550, 157336, 496243,
    129, 120, 98, 24428, 597, 329, 862, 12, 8587, 354912,
    313369, 244786, 120467, 76341, 419430, 37799, 68718, 16869, 1422, 769,
    807, 274, 348, 335984, 284054, 324857,
]


@onboarding_bp.get("/curated-movies")
@require_auth
@limiter.limit("10 per minute")
def curated_movies():
    def _fetch(mid: int):
        try:
            return movie_cache.get_movie(mid, segments=("core",))
        except Exception:
            return None

    try:
        movies = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(_fetch, mid) for mid in CURATED_MOVIE_IDS]
            for future in as_completed(futures):
                data = future.result()
                if data:
                    movies.append(data)

        # as_completed finishes out of order — restore the curated ordering.
        order = {mid: i for i, mid in enumerate(CURATED_MOVIE_IDS)}
        movies.sort(key=lambda m: order.get(m["id"], len(order)))
        return jsonify(movies)
    except Exception as exc:
        return server_error("Failed to fetch curated movies", exc, 500)
