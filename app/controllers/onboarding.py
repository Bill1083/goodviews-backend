from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Blueprint, current_app, jsonify, request

from app import limiter
from app.services import movie_cache, tmdb
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
    # Each worker thread needs its own pushed Flask app context —
    # movie_cache.get_movie relies on current_app (TTL config, TMDB API
    # key), which a ThreadPoolExecutor worker doesn't inherit by default.
    app = current_app._get_current_object()

    def _fetch(mid: int):
        with app.app_context():
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


def _parse_id_list(param: str, limit: int) -> list[int]:
    raw = request.args.get(param, "")
    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
        if len(ids) >= limit:
            break
    return ids


def _people_from_credits(data: dict) -> list[dict]:
    credits = data.get("credits") or {}
    people = []
    for c in (credits.get("cast") or [])[:3]:
        if c.get("id") and c.get("name"):
            people.append({"id": c["id"], "name": c["name"], "profile_path": c.get("profile_path"), "type": "actor"})
    for c in credits.get("crew") or []:
        if c.get("job") == "Director" and c.get("id") and c.get("name"):
            people.append(
                {"id": c["id"], "name": c["name"], "profile_path": c.get("profile_path"), "type": "director"}
            )
    return people


@onboarding_bp.get("/curated-people")
@require_auth
@limiter.limit("10 per minute")
def curated_people():
    """Actor/director suggestions for the favourites step — personalized
    from the genres picked in step 1 and the movies loved in step 2, topped
    up with generically popular people so the grid never looks sparse."""
    genre_ids = _parse_id_list("genre_ids", 3)
    movie_ids = _parse_id_list("movie_ids", 5)

    app = current_app._get_current_object()

    def _movies_for_genre(gid: int) -> list[int]:
        with app.app_context():
            try:
                data = tmdb.discover_movies({"with_genres": gid, "sort_by": "popularity.desc"})
            except Exception:
                return []
        return [m["id"] for m in data.get("results", [])[:2] if m.get("id")]

    def _fetch_movie(mid: int):
        with app.app_context():
            try:
                return movie_cache.get_movie(mid, segments=("core",))
            except Exception:
                return None

    def _fetch_popular():
        with app.app_context():
            try:
                return tmdb.get_popular_people(1)
            except Exception:
                return {}

    try:
        people: list[dict] = []
        seen: set[int] = set()

        def _merge(items: list[dict]) -> None:
            for p in items:
                if p["id"] not in seen:
                    seen.add(p["id"])
                    people.append(p)

        # Genre picks -> a couple of popular movies per genre -> their cast/director.
        genre_movie_ids: list[int] = []
        if genre_ids:
            with ThreadPoolExecutor(max_workers=len(genre_ids)) as executor:
                for movie_ids_for_genre in executor.map(_movies_for_genre, genre_ids):
                    genre_movie_ids.extend(movie_ids_for_genre)

        # Loved movies + genre-seeded movies -> cast/director, in that priority order
        # (movies the user actually reacted to are the stronger personalization signal).
        credit_movie_ids = list(dict.fromkeys(movie_ids + genre_movie_ids))
        if credit_movie_ids:
            with ThreadPoolExecutor(max_workers=min(len(credit_movie_ids), 10)) as executor:
                for data in executor.map(_fetch_movie, credit_movie_ids):
                    if data:
                        _merge(_people_from_credits(data))

        # Top up with generically popular people so the grid isn't sparse for
        # a brand-new profile with thin genre/movie signal.
        popular_data = _fetch_popular()
        popular_people = [
            {
                "id": p["id"],
                "name": p["name"],
                "profile_path": p.get("profile_path"),
                "type": "director" if p.get("known_for_department") == "Directing" else "actor",
            }
            for p in popular_data.get("results", [])
            if p.get("id") and p.get("name")
        ]
        _merge(popular_people)

        return jsonify(people[:30])
    except Exception as exc:
        return server_error("Failed to fetch curated people", exc, 500)
