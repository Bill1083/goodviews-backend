"""What members as a whole think of a film, for its public page.

Only totals ever leave here: how many members rated the film, the average,
and how the ratings spread across the five stars — never who rated it or
what they wrote. Below COMMUNITY_MIN_RATINGS only the count is returned, so
no single member's rating can be worked out from the page.
"""
from __future__ import annotations

import math

from app.services.pg import paginate

COMMUNITY_MIN_RATINGS = 3


def _star(rating: float) -> int:
    """1–5, rounding halves up (a 3.5 counts towards 4 stars)."""
    return min(5, max(1, math.floor(rating + 0.5)))


def latest_ratings(rows: list[dict]) -> list[float]:
    """One rating per member: their newest review of the film. A member can
    have more than one review row (POST /api/reviews always inserts)."""
    newest: dict[str, dict] = {}
    for row in rows:
        user_id, rating = row.get("user_id"), row.get("rating")
        if user_id is None or rating is None:
            continue
        current = newest.get(user_id)
        if current is None or (row.get("created_at") or "") > (current.get("created_at") or ""):
            newest[user_id] = row
    return [float(r["rating"]) for r in newest.values()]


def community_stats(supabase, movie_id: int) -> dict:
    rows = paginate(
        lambda: supabase.table("reviews").select("user_id, rating, created_at").eq("movie_id", movie_id).order("created_at")
    )
    ratings = latest_ratings(rows)
    count = len(ratings)
    if count < COMMUNITY_MIN_RATINGS:
        return {"movie_id": movie_id, "rating_count": count, "average_rating": None, "distribution": None}

    distribution = {str(star): 0 for star in range(1, 6)}
    for rating in ratings:
        distribution[str(_star(rating))] += 1
    return {
        "movie_id": movie_id,
        "rating_count": count,
        "average_rating": round(sum(ratings) / count, 1),
        "distribution": distribution,
    }
