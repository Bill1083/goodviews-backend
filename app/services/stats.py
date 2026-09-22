"""Taste dashboard + year-end "Wrapped" aggregation.

Everything a user's profile says about their taste is computed here, in
plain Python, from one embedded-join read of their reviews (plus watchlist,
favourites and friends' ratings on shared films). PostgREST has no GROUP BY
for us to lean on, and the volumes involved (hundreds of films per user) are
trivial in-process.

Module rules:
- No Flask at import time. Config values (unlock date, thresholds) and the
  clock/timezone are passed in by the controller, so the maths here is
  unit-testable without an app or a network.
- Every section of every payload is nullable/empty rather than absent, and
  the client hides what it can't show — a brand-new account gets a valid
  (mostly empty) dashboard, not a 500.
- Onboarding quick-ratings (reviews.is_onboarding) are real opinions about
  films the user saw *before* joining: they count towards all-time taste
  (genres, ratings, people) but never towards activity, streaks or a
  Wrapped year — matching how friends_recent_activity already treats them.
"""
from __future__ import annotations

import logging
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone, tzinfo
from typing import Any

from app.services.movie_cache import GENRE_MAP
from app.services.pg import IN_CHUNK, chunked, paginate

logger = logging.getLogger(__name__)

REVIEW_SELECT = (
    "id, movie_id, rating, review_text, rewatch_count, category_ids, is_onboarding, created_at, "
    "movies(id, title, poster_path, backdrop_path, release_date, runtime, genre_ids, vote_average, "
    "vote_count, popularity, original_language, production_countries, budget, revenue, "
    "collection_id, collection_name, tagline, directors, top_cast)"
)
# Pre-sql/008 column set. If the migration hasn't been applied yet the stats
# still work (minus people/extras) instead of the profile page erroring.
LEGACY_REVIEW_SELECT = (
    "id, movie_id, rating, review_text, rewatch_count, category_ids, is_onboarding, created_at, "
    "movies(id, title, poster_path, backdrop_path, release_date, runtime, genre_ids, vote_average)"
)
WATCHLIST_SELECT = (
    "movie_id, added_at, movies(id, title, poster_path, backdrop_path, release_date, runtime, genre_ids, vote_average)"
)
FRIENDS_SELECT = (
    "friend_id, profiles!friendships_friend_id_fkey(id, username, avatar_url, avatar_color, avatar_focal_y, avatar_zoom)"
)
FRIEND_REVIEW_SELECT = "user_id, movie_id, rating, created_at"

FRIEND_ID_CHUNK = 150

# Comparisons against TMDB's crowd score only count when the crowd is big
# enough to mean something and the gap is wide enough to be a real "take".
MIN_VOTE_COUNT_FOR_TAKES = 50
HOT_TAKE_MIN_DELTA = 1.5
AGREEMENT_MAX_DELTA = 2.0  # one star on the 5-star scale, i.e. two points on TMDB's 10
MIN_SHARED_FOR_COMPAT = 3
BLOCKBUSTER_BUDGET = 100_000_000
INDIE_BUDGET = 5_000_000
HIDDEN_GEM_MAX_POPULARITY = 15.0
EXTRAS_MIN_FILMS = 10
EXTRAS_MIN_COVERAGE = 0.5
NIGHT_HOURS = {22, 23, 0, 1, 2, 3}


# ─── Data shapes ─────────────────────────────────────────────────────────────

@dataclass
class FilmRow:
    """One reviewed film with everything the stats need, typed and defaulted."""
    review_id: str
    movie_id: int
    rating: float
    review_text: str
    rewatch_count: int
    category_ids: list[str]
    is_onboarding: bool
    created_at: datetime  # aware, UTC
    title: str
    poster_path: str | None
    backdrop_path: str | None
    release_date: str | None
    year: int | None
    runtime: int | None
    genre_ids: list[int]
    vote_average: float | None
    vote_count: int | None
    popularity: float | None
    original_language: str | None
    production_countries: list[str]
    budget: int | None
    revenue: int | None
    collection_id: int | None
    collection_name: str | None
    directors: list[dict] | None
    top_cast: list[dict] | None
    words: int

    @property
    def has_extras(self) -> bool:
        return self.original_language is not None

    @property
    def has_people(self) -> bool:
        return self.directors is not None


@dataclass
class TasteData:
    user_id: str
    reviews: list[dict] = field(default_factory=list)
    watchlist: list[dict] = field(default_factory=list)
    # Friends are only loaded for the Wrapped (load_taste_data's include_social).
    friends: list[dict] = field(default_factory=list)
    friend_reviews: list[dict] = field(default_factory=list)


# ─── Loading ─────────────────────────────────────────────────────────────────

def _load_reviews(user_id: str, supabase) -> list[dict]:
    def query(select: str):
        return lambda: (
            supabase.table("reviews")
            .select(select)
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .order("id")
        )

    try:
        return paginate(query(REVIEW_SELECT))
    except Exception as exc:
        # PostgREST rejects a select naming a column that doesn't exist yet
        # (42703 / "does not exist") — sql/008 not applied. Degrade rather
        # than take the whole profile page down.
        message = str(exc)
        if "42703" not in message and "does not exist" not in message and "column" not in message.lower():
            raise
        logger.error("movies table is missing the sql/008 stats columns - apply the migration. Loading legacy columns only.")
        return paginate(query(LEGACY_REVIEW_SELECT))


def load_taste_data(user_id: str, supabase, *, include_social: bool = True) -> TasteData:
    """All the rows one user's stats are computed from. Paginated where a
    table can exceed PostgREST's 1000-row cap; .in_() lists are chunked so
    URLs stay short. Friends' ratings are only fetched for films the user
    has rated themselves — nothing about a friend's *other* films is loaded.

    `include_social=False` skips the friend queries entirely: the profile
    dashboard has no friend section (that comparison is a Wrapped reveal),
    and those chunked lookups are the slowest part of the load."""
    reviews = _load_reviews(user_id, supabase)
    watchlist = paginate(
        lambda: supabase.table("watchlist")
        .select(WATCHLIST_SELECT)
        .eq("user_id", user_id)
        .order("added_at", desc=True)
        .order("movie_id")
    )
    if not include_social:
        return TasteData(user_id=user_id, reviews=reviews, watchlist=watchlist)

    friend_rows = (
        supabase.table("friendships")
        .select(FRIENDS_SELECT)
        .eq("user_id", user_id)
        .execute()
        .data
        or []
    )
    friends = [
        {
            "id": r["profiles"]["id"],
            "username": r["profiles"].get("username"),
            "avatar_url": r["profiles"].get("avatar_url"),
            "avatar_color": r["profiles"].get("avatar_color"),
            "avatar_focal_y": r["profiles"].get("avatar_focal_y"),
            "avatar_zoom": r["profiles"].get("avatar_zoom"),
        }
        for r in friend_rows
        if r.get("profiles")
    ]

    friend_reviews: list[dict] = []
    movie_ids = sorted({r["movie_id"] for r in reviews if r.get("movie_id") is not None})
    friend_ids = [f["id"] for f in friends]
    if friend_ids and movie_ids:
        for fchunk in chunked(friend_ids, FRIEND_ID_CHUNK):
            for mchunk in chunked(movie_ids, IN_CHUNK):
                friend_reviews.extend(
                    paginate(
                        lambda f=fchunk, m=mchunk: supabase.table("reviews")
                        .select(FRIEND_REVIEW_SELECT)
                        .in_("user_id", f)
                        .in_("movie_id", m)
                        .order("created_at", desc=True)
                        .order("id")
                    )
                )

    return TasteData(
        user_id=user_id,
        reviews=reviews,
        watchlist=watchlist,
        friends=friends,
        friend_reviews=friend_reviews,
    )


def load_review_dates(user_id: str, supabase) -> list[datetime]:
    """One timestamp per film (the newest organic review of it) — enough to
    say which years have a Wrapped and how many films each holds, without
    loading any film data. Deduplicated the same way film_rows() is, so the
    hub card and the Wrapped itself agree on the count."""
    rows = paginate(
        lambda: supabase.table("reviews")
        .select("movie_id, created_at")
        .eq("user_id", user_id)
        .eq("is_onboarding", False)
        .order("created_at", desc=True)
        .order("id")
    )
    seen: set[int] = set()
    dates: list[datetime] = []
    for r in rows:  # newest first, so the first sighting of a movie wins
        movie_id = _as_int(r.get("movie_id"))
        dt = _parse_dt(r.get("created_at"))
        if movie_id is None or dt is None or movie_id in seen:
            continue
        seen.add(movie_id)
        dates.append(dt)
    return dates


# ─── Normalisation ───────────────────────────────────────────────────────────

def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == value:
        return int(value)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _release_year(release_date: Any) -> int | None:
    if not release_date:
        return None
    year = _as_int(str(release_date)[:4])
    return year if year and 1870 <= year <= 2100 else None


def _people_list(value: Any) -> list[dict] | None:
    if not isinstance(value, list):
        return None
    return [p for p in value if isinstance(p, dict) and isinstance(p.get("id"), int)]


def film_rows(raw_reviews: list[dict]) -> list[FilmRow]:
    """Typed rows, one per film. Duplicate (user, movie) review rows are
    possible (POST /api/reviews always inserts), so the newest review of a
    film wins — the same choice GET /api/reviews/movie/<id> makes. Rows
    whose movie is missing from the cache are skipped."""
    newest: dict[int, FilmRow] = {}
    for raw in raw_reviews:
        movie = raw.get("movies")
        movie_id = _as_int(raw.get("movie_id"))
        created_at = _parse_dt(raw.get("created_at"))
        rating = _as_float(raw.get("rating"))
        if not isinstance(movie, dict) or movie_id is None or created_at is None or rating is None:
            continue
        text = (raw.get("review_text") or "").strip()
        runtime = _as_int(movie.get("runtime"))
        row = FilmRow(
            review_id=str(raw.get("id")),
            movie_id=movie_id,
            rating=max(1.0, min(5.0, rating)),
            review_text=text,
            rewatch_count=max(0, _as_int(raw.get("rewatch_count")) or 0),
            category_ids=[str(c) for c in (raw.get("category_ids") or []) if c],
            is_onboarding=bool(raw.get("is_onboarding")),
            created_at=created_at,
            title=movie.get("title") or "Untitled",
            poster_path=movie.get("poster_path"),
            backdrop_path=movie.get("backdrop_path"),
            release_date=movie.get("release_date"),
            year=_release_year(movie.get("release_date")),
            runtime=runtime if runtime and runtime > 0 else None,
            genre_ids=[g for g in (movie.get("genre_ids") or []) if isinstance(g, int)],
            vote_average=_as_float(movie.get("vote_average")),
            vote_count=_as_int(movie.get("vote_count")),
            popularity=_as_float(movie.get("popularity")),
            original_language=movie.get("original_language") or None,
            production_countries=[c for c in (movie.get("production_countries") or []) if isinstance(c, str)],
            budget=_as_int(movie.get("budget")) or None,
            revenue=_as_int(movie.get("revenue")) or None,
            collection_id=_as_int(movie.get("collection_id")),
            collection_name=movie.get("collection_name") or None,
            directors=_people_list(movie.get("directors")),
            top_cast=_people_list(movie.get("top_cast")),
            words=len(text.split()) if text else 0,
        )
        current = newest.get(movie_id)
        if current is None or row.created_at > current.created_at:
            newest[movie_id] = row
    return sorted(newest.values(), key=lambda f: f.created_at, reverse=True)


# ─── Small helpers ───────────────────────────────────────────────────────────

def _avg(values: list[float], digits: int = 2) -> float | None:
    return round(sum(values) / len(values), digits) if values else None


def _share(part: int, whole: int, digits: int = 3) -> float:
    return round(part / whole, digits) if whole else 0.0


def _film_ref(f: FilmRow) -> dict:
    return {
        "id": f.movie_id,
        "title": f.title,
        "poster_path": f.poster_path,
        "backdrop_path": f.backdrop_path,
        "release_date": f.release_date,
    }


def _rated(f: FilmRow) -> dict:
    return {"movie": _film_ref(f), "rating": f.rating, "rewatch_count": f.rewatch_count}


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _week_index(d: date) -> int:
    # 0001-01-01 is a Monday in the proleptic Gregorian calendar, so this
    # numbers Monday-aligned weeks consistently.
    return (d.toordinal() - 1) // 7


def _streaks(days: list[date], today: date) -> tuple[int, int]:
    """(current, longest) runs of consecutive calendar weeks with at least
    one film logged. The current run may end this week or last week — a
    quiet Monday shouldn't read as a broken streak."""
    weeks = sorted({_week_index(d) for d in days})
    if not weeks:
        return 0, 0
    longest = run = 1
    for prev, cur in zip(weeks, weeks[1:]):
        run = run + 1 if cur == prev + 1 else 1
        longest = max(longest, run)
    this_week = _week_index(today)
    if weeks[-1] not in (this_week, this_week - 1):
        return 0, longest
    current = 1
    i = len(weeks) - 1
    while i > 0 and weeks[i - 1] == weeks[i] - 1:
        current += 1
        i -= 1
    return current, longest


def _delta_vs_world(f: FilmRow) -> float | None:
    """Your stars, doubled onto TMDB's 10-point scale, minus the crowd's
    score. None when the crowd is too small (or absent) to compare against."""
    if not f.vote_average or f.vote_average <= 0:
        return None
    if f.vote_count is not None and f.vote_count < MIN_VOTE_COUNT_FOR_TAKES:
        return None
    return round(f.rating * 2 - f.vote_average, 2)


def _take(f: FilmRow, delta: float) -> dict:
    return {"movie": _film_ref(f), "your_rating": f.rating, "tmdb": round(f.vote_average or 0, 1), "delta": delta}


# ─── Section builders (shared by dashboard + Wrapped) ────────────────────────

def _genre_table(films: list[FilmRow]) -> list[dict]:
    counts: Counter[int] = Counter()
    ratings: dict[int, list[float]] = defaultdict(list)
    affinity: dict[int, float] = defaultdict(float)
    for f in films:
        for gid in set(f.genre_ids):
            if gid not in GENRE_MAP:
                continue
            counts[gid] += 1
            ratings[gid].append(f.rating)
            affinity[gid] += f.rating - 3  # centred on "It was okay", as MyMoviesPage does
    total = len(films)
    table = [
        {
            "id": gid,
            "name": GENRE_MAP[gid],
            "count": n,
            "share": _share(n, total),
            "avg_rating": _avg(ratings[gid]),
            "affinity": round(affinity[gid], 2),
        }
        for gid, n in counts.items()
    ]
    return sorted(table, key=lambda g: (-g["count"], -(g["avg_rating"] or 0), g["name"]))


def _genre_highlights(genres: list[dict]) -> dict:
    rated = [g for g in genres if g["count"] >= 3 and g["avg_rating"] is not None]
    by_avg = sorted(rated, key=lambda g: (-g["avg_rating"], -g["count"]))
    highest = by_avg[0] if by_avg else None
    lowest = by_avg[-1] if len(by_avg) >= 2 and by_avg[-1] is not highest else None
    return {
        "most_watched": dict(genres[0]) if genres else None,
        "highest_rated": dict(highest) if highest else None,
        "lowest_rated": dict(lowest) if lowest else None,
        "affinity_top": [dict(g) for g in sorted(genres, key=lambda g: -g["affinity"]) if g["affinity"] > 0][:3],
    }


def _decades(films: list[FilmRow]) -> list[dict]:
    buckets: dict[int, list[float]] = defaultdict(list)
    for f in films:
        if f.year:
            buckets[(f.year // 10) * 10].append(f.rating)
    return [
        {"decade": decade, "count": len(r), "avg_rating": _avg(r)}
        for decade, r in sorted(buckets.items())
    ]


def _eras(films: list[FilmRow]) -> dict:
    dated = [f for f in films if f.year]
    if not dated:
        return {"oldest": None, "newest": None, "mean_release_year": None}
    oldest = min(dated, key=lambda f: (f.year, f.release_date or "", f.title))
    newest = max(dated, key=lambda f: (f.year, f.release_date or "", f.title))
    return {
        "oldest": {**_film_ref(oldest), "year": oldest.year, "rating": oldest.rating},
        "newest": {**_film_ref(newest), "year": newest.year, "rating": newest.rating},
        "mean_release_year": round(sum(f.year for f in dated) / len(dated), 1),
    }


def _person_table(films: list[FilmRow], attr: str) -> list[dict]:
    """Per-person aggregates over `directors` or `top_cast`."""
    entries: dict[int, dict] = {}
    for f in films:
        people = getattr(f, attr) or []
        seen: set[int] = set()
        for p in people:
            pid = p["id"]
            if pid in seen:
                continue
            seen.add(pid)
            e = entries.setdefault(pid, {
                "id": pid, "name": p.get("name") or "Unknown", "profile_path": p.get("profile_path"),
                "count": 0, "_ratings": [], "_films": [],
            })
            e["count"] += 1
            e["_ratings"].append(f.rating)
            e["_films"].append(f)
            if not e["profile_path"] and p.get("profile_path"):
                e["profile_path"] = p["profile_path"]
    table = []
    for e in entries.values():
        best_first = sorted(e["_films"], key=lambda f: (-f.rating, -f.rewatch_count, f.created_at))
        table.append({
            "id": e["id"],
            "name": e["name"],
            "profile_path": e["profile_path"],
            "count": e["count"],
            "avg_rating": _avg(e["_ratings"]),
            "films": [_film_ref(f) for f in best_first[:4]],
        })
    return table


def _people(films: list[FilmRow]) -> dict:
    def block(attr: str) -> dict:
        table = _person_table(films, attr)
        most = sorted(table, key=lambda p: (-p["count"], -(p["avg_rating"] or 0), p["name"]))[:8]
        best = sorted(
            (p for p in table if p["count"] >= 2),
            key=lambda p: (-(p["avg_rating"] or 0), -p["count"], p["name"]),
        )[:5]
        return {"most_watched": most, "highest_rated": best}
    return {"directors": block("directors"), "actors": block("top_cast")}


def _rewatches(films: list[FilmRow], limit: int = 6) -> dict:
    rewatched = sorted((f for f in films if f.rewatch_count > 0), key=lambda f: (-f.rewatch_count, -f.rating, f.title))
    return {"total": sum(f.rewatch_count for f in films), "top": [_rated(f) for f in rewatched[:limit]]}


def _vs_world(films: list[FilmRow]) -> dict:
    pairs = [(f, d) for f, d in ((f, _delta_vs_world(f)) for f in films) if d is not None]
    if not pairs:
        return {"mean_delta": None, "label": None, "agreement_share": None, "sample_size": 0,
                "hot_takes": {"loved_more": [], "loved_less": []}}
    deltas = [d for _, d in pairs]
    mean_delta = round(sum(deltas) / len(deltas), 2)
    label = "kinder" if mean_delta >= 0.5 else "harsher" if mean_delta <= -0.5 else "in step"
    agree = sum(1 for d in deltas if abs(d) < AGREEMENT_MAX_DELTA)
    loved_more = sorted((p for p in pairs if p[1] >= HOT_TAKE_MIN_DELTA), key=lambda p: -p[1])[:3]
    loved_less = sorted((p for p in pairs if p[1] <= -HOT_TAKE_MIN_DELTA), key=lambda p: p[1])[:3]
    return {
        "mean_delta": mean_delta,
        "label": label,
        "agreement_share": _share(agree, len(deltas)),
        "sample_size": len(deltas),
        "hot_takes": {
            "loved_more": [_take(f, d) for f, d in loved_more],
            "loved_less": [_take(f, d) for f, d in loved_less],
        },
    }


def _runtime(films: list[FilmRow]) -> dict:
    timed = [f for f in films if f.runtime]
    if not timed:
        return {"avg_minutes": None, "longest": None, "shortest": None,
                "share_over_2h": 0.0, "share_under_90m": 0.0, "sample_size": 0}
    longest = max(timed, key=lambda f: (f.runtime, f.rating))
    shortest = min(timed, key=lambda f: (f.runtime, -f.rating))
    return {
        "avg_minutes": round(sum(f.runtime for f in timed) / len(timed)),
        "longest": {**_film_ref(longest), "runtime": longest.runtime, "rating": longest.rating},
        "shortest": {**_film_ref(shortest), "runtime": shortest.runtime, "rating": shortest.rating},
        "share_over_2h": _share(sum(1 for f in timed if f.runtime >= 120), len(timed)),
        "share_under_90m": _share(sum(1 for f in timed if f.runtime < 90), len(timed)),
        "sample_size": len(timed),
    }


def _watchlist(data: TasteData, films: list[FilmRow], now: datetime) -> dict:
    items = []
    for raw in data.watchlist:
        movie = raw.get("movies")
        added = _parse_dt(raw.get("added_at"))
        if not isinstance(movie, dict) or added is None:
            continue
        items.append((raw, movie, added))
    if not items:
        return {"count": 0, "total_minutes": 0, "oldest": None, "genre_gap": []}

    oldest_raw, oldest_movie, oldest_added = min(items, key=lambda t: t[2])
    total_minutes = sum(m.get("runtime") or 0 for _, m, _ in items)

    gap: list[dict] = []
    if len(items) >= 3 and films:
        wl_counts: Counter[int] = Counter()
        for _, m, _ in items:
            for gid in set(g for g in (m.get("genre_ids") or []) if isinstance(g, int)):
                if gid in GENRE_MAP:
                    wl_counts[gid] += 1
        watched_counts: Counter[int] = Counter()
        for f in films:
            for gid in set(f.genre_ids):
                if gid in GENRE_MAP:
                    watched_counts[gid] += 1
        for gid, n in wl_counts.items():
            if n < 2:
                continue
            wl_share = _share(n, len(items))
            watched_share = _share(watched_counts.get(gid, 0), len(films))
            if wl_share > watched_share:
                gap.append({"id": gid, "name": GENRE_MAP[gid], "watchlist_share": wl_share, "watched_share": watched_share})
        gap.sort(key=lambda g: -(g["watchlist_share"] - g["watched_share"]))

    return {
        "count": len(items),
        "total_minutes": total_minutes,
        "oldest": {
            "movie": {
                "id": oldest_movie.get("id"),
                "title": oldest_movie.get("title"),
                "poster_path": oldest_movie.get("poster_path"),
                "backdrop_path": oldest_movie.get("backdrop_path"),
                "release_date": oldest_movie.get("release_date"),
            },
            "added_at": _iso(oldest_added),
            "days_waiting": max(0, (now.astimezone(timezone.utc) - oldest_added).days),
        },
        "genre_gap": gap[:3],
    }


def _friend_ratings(data: TasteData) -> dict[str, dict[int, float]]:
    """{friend_id: {movie_id: rating}} — newest rating per film wins (rows
    arrive newest-first)."""
    ratings: dict[str, dict[int, float]] = defaultdict(dict)
    for r in data.friend_reviews:
        uid, mid, rating = r.get("user_id"), _as_int(r.get("movie_id")), _as_float(r.get("rating"))
        if not uid or mid is None or rating is None or mid in ratings[uid]:
            continue
        ratings[uid][mid] = rating
    return ratings


def _friends(data: TasteData, films: list[FilmRow]) -> dict:
    mine = {f.movie_id: f for f in films}
    theirs = _friend_ratings(data)
    compared = []
    for friend in data.friends:
        ratings = theirs.get(friend["id"], {})
        shared = [mid for mid in ratings if mid in mine]
        if len(shared) < MIN_SHARED_FOR_COMPAT:
            continue
        diffs = [(mid, mine[mid].rating - ratings[mid]) for mid in shared]
        mad = sum(abs(d) for _, d in diffs) / len(diffs)
        worst_mid, worst_diff = max(diffs, key=lambda t: (abs(t[1]), -t[0]))
        compared.append({
            **friend,
            "shared_count": len(shared),
            "compatibility": round(max(0.0, 1 - mad / 4), 3),
            "mean_abs_diff": round(mad, 2),
            "most_disagreed": None if abs(worst_diff) < 1 else {
                "movie": _film_ref(mine[worst_mid]),
                "your_rating": mine[worst_mid].rating,
                "their_rating": ratings[worst_mid],
            },
        })
    compared.sort(key=lambda c: (-c["compatibility"], -c["shared_count"], c["username"] or ""))
    twin = compared[0] if compared else None
    nemesis = compared[-1] if len(compared) >= 2 else None
    return {"friend_count": len(data.friends), "compared": compared, "twin": twin, "nemesis": nemesis}


def _extras(films: list[FilmRow]) -> dict | None:
    with_extras = [f for f in films if f.has_extras]
    if len(with_extras) < EXTRAS_MIN_FILMS or len(with_extras) < EXTRAS_MIN_COVERAGE * len(films):
        return None

    langs = Counter(f.original_language for f in with_extras if f.original_language)
    countries: Counter[str] = Counter()
    for f in with_extras:
        for code in set(f.production_countries):
            countries[code] += 1
    budgets = sorted(f.budget for f in with_extras if f.budget)
    gems = sorted(
        (f for f in with_extras if f.rating >= 4 and f.popularity is not None and f.popularity < HIDDEN_GEM_MAX_POPULARITY),
        key=lambda f: (f.popularity, -f.rating),
    )
    franchises: dict[int, list[FilmRow]] = defaultdict(list)
    for f in with_extras:
        if f.collection_id is not None:
            franchises[f.collection_id].append(f)
    franchise_rows = [
        {
            "collection_id": cid,
            "name": next((f.collection_name for f in group if f.collection_name), "Franchise"),
            "count": len(group),
            "avg_rating": _avg([f.rating for f in group]),
            "films": [_film_ref(f) for f in sorted(group, key=lambda f: (f.release_date or "", f.title))[:4]],
        }
        for cid, group in franchises.items()
        if len(group) >= 2
    ]
    franchise_rows.sort(key=lambda r: (-r["count"], -(r["avg_rating"] or 0), r["name"]))

    return {
        "sample_size": len(with_extras),
        "languages": {
            "count": len(langs),
            "top": [{"code": code, "count": n, "share": _share(n, len(with_extras))} for code, n in langs.most_common(5)],
            "non_english_share": _share(sum(n for code, n in langs.items() if code != "en"), len(with_extras)),
        },
        "countries": {
            "count": len(countries),
            "top": [{"code": code, "count": n} for code, n in countries.most_common(6)],
        },
        "budget": {
            "blockbuster_count": sum(1 for b in budgets if b >= BLOCKBUSTER_BUDGET),
            "indie_count": sum(1 for b in budgets if b < INDIE_BUDGET),
            "median_budget": int(statistics.median(budgets)) if budgets else None,
            "sample_size": len(budgets),
        },
        "hidden_gems": [{**_rated(f), "popularity": round(f.popularity, 1)} for f in gems[:6]],
        "franchises": franchise_rows[:5],
    }


# ─── Dashboard ───────────────────────────────────────────────────────────────

def compute_dashboard(data: TasteData, *, now: datetime, tz: tzinfo) -> dict:
    """The profile's taste card: a few plain totals plus the genre breakdown.

    Everything with a reveal in it — favourite films, most-watched people,
    eras, rewatches, hot takes, taste twins, the year's rhythm — is
    deliberately absent, and absent from the *payload* rather than merely
    hidden in the UI, so a curious user can't read their Wrapped out of the
    network tab either. Genres are the exception: the Taste DNA lives on the
    profile by design, and the Wrapped's genre slide is a year-scoped view of
    the same thing rather than a spoiler."""
    films = film_rows(data.reviews)
    organic = [f for f in films if not f.is_onboarding]
    genres = _genre_table(films)
    watchlist = _watchlist(data, films, now)
    first = min((f.created_at for f in organic), default=None) or min((f.created_at for f in films), default=None)
    last = max((f.created_at for f in organic), default=None) or max((f.created_at for f in films), default=None)

    return {
        "generated_at": _iso(now.astimezone(timezone.utc)),
        "tz": getattr(tz, "key", None) or str(tz),
        "coverage": {
            "films": len(films),
            "with_runtime": sum(1 for f in films if f.runtime),
            "with_people": sum(1 for f in films if f.has_people),
            "with_extras": sum(1 for f in films if f.has_extras),
        },
        "headline": {
            "films": len(films),
            "films_excluding_onboarding": len(organic),
            "watch_minutes": sum(f.runtime * (1 + f.rewatch_count) for f in films if f.runtime),
            "avg_rating": _avg([f.rating for f in films]),
            "rewatches": sum(f.rewatch_count for f in films),
            "rewatched_films": sum(1 for f in films if f.rewatch_count > 0),
            "written_reviews": sum(1 for f in films if f.words),
            "written_words": sum(f.words for f in films),
            "first_rated_at": _iso(first),
            "last_rated_at": _iso(last),
        },
        "genres": genres,
        "genre_highlights": _genre_highlights(genres),
        # Count and runtime only: which films are on the list, and how long the
        # oldest has been waiting, are a Wrapped slide (and the list itself is
        # already on My Movies).
        "watchlist": {"count": watchlist["count"], "total_minutes": watchlist["total_minutes"]},
    }


# ─── Wrapped ─────────────────────────────────────────────────────────────────

PERSONAS: dict[str, dict] = {
    "marathoner": {
        "title": "The Marathoner",
        "tagline": "Sleep is for the intermission.",
        "description": "You didn't watch films this year — you consumed them. Volume, stamina, dedication.",
        "emoji": "🏃",
        "theme": "neon",
    },
    "explorer": {
        "title": "The Explorer",
        "tagline": "No genre left unturned.",
        "description": "Comedies, horrors, documentaries, films from half the planet — you went everywhere.",
        "emoji": "🧭",
        "theme": "aurora",
    },
    "archaeologist": {
        "title": "The Archaeologist",
        "tagline": "Digging up the classics.",
        "description": "New releases can wait. Your year lived in decades most people have forgotten.",
        "emoji": "🏺",
        "theme": "sepia",
    },
    "premiere_chaser": {
        "title": "The Premiere Chaser",
        "tagline": "First in line, every time.",
        "description": "If it came out this year, you saw it — probably before your friends did.",
        "emoji": "🎟️",
        "theme": "ember",
    },
    "devotee": {
        "title": "The Devotee",
        "tagline": "You know exactly what you love.",
        "description": "One genre owned your year, and honestly, you wouldn't have it any other way.",
        "emoji": "💘",
        "theme": "sunset",
    },
    "harsh_critic": {
        "title": "The Harsh Critic",
        "tagline": "Stars are earned, not given.",
        "description": "Plenty of films crossed your screen. Very few impressed you. That's the point.",
        "emoji": "🧐",
        "theme": "noir",
    },
    "enthusiast": {
        "title": "The Enthusiast",
        "tagline": "Every film is a good time.",
        "description": "You loved most of what you watched this year — an infectious way to go through a year of film.",
        "emoji": "🍿",
        "theme": "gold",
    },
    "contrarian": {
        "title": "The Contrarian",
        "tagline": "The crowd is usually wrong.",
        "description": "Where the world saw a masterpiece, you saw a Tuesday. And vice versa. Repeatedly.",
        "emoji": "🙃",
        "theme": "ember",
    },
    "loyalist": {
        "title": "The Loyalist",
        "tagline": "Again. And again.",
        "description": "You return to what you love — the same director, the same film, one more time.",
        "emoji": "🔁",
        "theme": "ocean",
    },
    "wordsmith": {
        "title": "The Wordsmith",
        "tagline": "Your reviews could fill a zine.",
        "description": "Rating wasn't enough. You had things to say, and you said them at length.",
        "emoji": "✍️",
        "theme": "sepia",
    },
    "night_owl": {
        "title": "The Night Owl",
        "tagline": "The best films start after midnight.",
        "description": "Most of your year in film happened while everyone else was asleep.",
        "emoji": "🦉",
        "theme": "noir",
    },
    "completionist": {
        "title": "The Completionist",
        "tagline": "Every entry. In order.",
        "description": "Franchises aren't a suggestion to you, they're a checklist.",
        "emoji": "📚",
        "theme": "ocean",
    },
    "cinephile": {
        "title": "The Cinephile",
        "tagline": "A bit of everything, and all of it worth watching.",
        "description": "No single obsession — just a well-rounded year of really good films.",
        "emoji": "🎬",
        "theme": "aurora",
    },
}

# Each slide kind gets a fixed look; the persona/summary pick up the
# persona's own theme so the ending feels earned.
SLIDE_THEMES = {
    "intro": "aurora", "volume": "neon", "months": "ocean", "genres": "sunset", "eras": "sepia",
    "people": "aurora", "loves": "gold", "hates": "noir", "hot_take": "ember", "critic": "ocean",
    "rewatches": "neon", "words": "sepia", "watchlist": "ocean", "friends": "sunset",
    "runtime": "sunset",
    "hidden_gem": "aurora", "world": "sunset",
}


def _pct(share: float) -> int:
    return int(round(share * 100))


def _stars(rating: float | None) -> str:
    return f"{rating:.1f} stars" if rating is not None else "no ratings"


def pick_persona(m: dict, year: int) -> dict:
    """Rule-based archetype. Fixed priority, first match wins — the rarer,
    more distinctive traits are checked first so a 120-film year that is
    also slightly harsh reads as a Marathoner, not a Critic. Every rule
    contributes the evidence line that earned it."""
    top_genre = m.get("top_genre")
    checks: list[tuple[str, bool, str]] = [
        ("marathoner", m["minutes"] >= 6000 or m["films"] >= 100,
         f"{m['films']} films and roughly {round(m['minutes'] / 60)} hours in {year}."),
        ("explorer", m["genre_count"] >= 10 or (m["country_count"] or 0) >= 6,
         (f"{m['genre_count']} different genres" + (f" and films from {m['country_count']} countries" if (m["country_count"] or 0) >= 6 else "") + ".")),
        ("archaeologist", (m["mean_release_year"] is not None and m["mean_release_year"] <= year - 25) or m["pre2000_share"] >= 0.5,
         (f"Your average film was released in {int(m['mean_release_year'])}." if m["mean_release_year"] else f"{_pct(m['pre2000_share'])}% of your films were made before 2000.")),
        ("premiere_chaser", m["recent_share"] >= 0.5,
         f"{_pct(m['recent_share'])}% of what you watched was brand new."),
        ("devotee", m["top_genre_share"] >= 0.5 and top_genre is not None,
         f"{_pct(m['top_genre_share'])}% of your year was {top_genre}."),
        ("harsh_critic", (m["avg_rating"] is not None and m["avg_rating"] <= 2.8) or m["disliked_share"] >= 0.4,
         f"Your average rating was {_stars(m['avg_rating'])}, and {_pct(m['disliked_share'])}% of films got 2 stars or fewer."),
        ("enthusiast", m["loved_share"] >= 0.6,
         f"You loved {_pct(m['loved_share'])}% of everything you watched."),
        ("contrarian", m["agreement_share"] is not None and m["agreement_share"] <= 0.4 and m["agreement_sample"] >= 10,
         f"You only agreed with the crowd on {_pct(m['agreement_share'])}% of films."),
        ("loyalist", m["rewatches"] >= 5 or m["max_director_count"] >= 4,
         (f"{m['rewatches']} rewatches this year." if m["rewatches"] >= 5 else f"{m['max_director_count']} films by {m['top_director']}.")),
        ("wordsmith", m["words"] >= 2000,
         f"{m['words']:,} words of reviews."),
        ("night_owl", m["night_share"] >= 0.4 and m["films"] >= 8,
         f"{_pct(m['night_share'])}% of your films were logged between 10pm and 4am."),
        ("completionist", m["franchises_3plus"] >= 2,
         f"{m['franchises_3plus']} franchises with three or more entries watched."),
    ]
    key, evidence = "cinephile", f"{m['films']} films across {m['genre_count']} genres in {year}."
    for candidate, fired, line in checks:
        if fired:
            key, evidence = candidate, line
            break

    extra: list[str] = []
    if top_genre and key != "devotee":
        extra.append(f"{top_genre} was your most-watched genre.")
    if m["avg_rating"] is not None and key not in ("harsh_critic", "enthusiast"):
        extra.append(f"Average rating: {_stars(m['avg_rating'])}.")
    if m["top_director"] and key != "loyalist":
        extra.append(f"Most-watched director: {m['top_director']}.")
    persona = PERSONAS[key]
    return {"key": key, **persona, "evidence": [evidence, *extra[:2]]}


def _month_day(value: str) -> tuple[int, int]:
    try:
        month, day = (int(part) for part in value.strip().split("-", 1))
        datetime(2001, month, day)  # validates the combination
        return month, day
    except (TypeError, ValueError):
        logger.warning("Invalid WRAPPED_UNLOCK_MONTH_DAY %r — falling back to 12-01", value)
        return 12, 1


def unlock_status(year: int, *, now: datetime, unlock_month_day: str, preview: bool = False) -> tuple[str, datetime | None]:
    """('ready' | 'locked' | 'future', unlocks_at). Past years are always
    ready; the current year unlocks at 00:00 UTC on the configured
    month-day — UTC on purpose, so nothing the client sends can move it.
    `preview` (local dev only) forces the current year open."""
    now_utc = now.astimezone(timezone.utc)
    if year > now_utc.year:
        return "future", None
    if year < now_utc.year or preview:
        return "ready", None
    month, day = _month_day(unlock_month_day)
    unlocks_at = datetime(year, month, day, tzinfo=timezone.utc)
    if now_utc >= unlocks_at:
        return "ready", None
    return "locked", unlocks_at


def wrapped_years(review_dates: list[datetime], tz: tzinfo) -> list[int]:
    """Distinct years (in the user's local time) with at least one organic
    review, newest first."""
    return sorted({dt.astimezone(tz).year for dt in review_dates}, reverse=True)


def compute_wrapped(data: TasteData, year: int, *, now: datetime, tz: tzinfo, min_films: int = 5) -> dict:
    """The year's story: an ordered list of slides, a persona and a summary.
    Only organic reviews logged during `year` (local time) count."""
    all_films = film_rows(data.reviews)
    films = [f for f in all_films if not f.is_onboarding and f.created_at.astimezone(tz).year == year]
    generated_at = _iso(now.astimezone(timezone.utc))
    tz_name = getattr(tz, "key", None) or str(tz)

    if len(films) < min_films:
        return {"status": "not_enough", "year": year, "films": len(films), "min_films": min_films,
                "generated_at": generated_at, "tz": tz_name}

    by_date = sorted(films, key=lambda f: f.created_at)
    local_times = [f.created_at.astimezone(tz) for f in films]
    minutes = sum(f.runtime for f in films if f.runtime)
    genres = _genre_table(films)
    people = _people(films)
    vs_world = _vs_world(films)
    eras = _eras(films)
    now_local = now.astimezone(tz)
    slides: list[dict] = []

    def add(kind: str, payload: dict) -> None:
        slides.append({"kind": kind, "theme": SLIDE_THEMES.get(kind, "aurora"), **payload})

    # intro
    poster_wall = [f.poster_path for f in sorted(films, key=lambda f: (-f.rating, f.created_at)) if f.poster_path][:12]
    add("intro", {
        "films": len(films),
        "first_film": _film_ref(by_date[0]),
        "first_rated_at": _iso(by_date[0].created_at),
        "poster_wall": poster_wall,
    })

    # volume
    add("volume", {
        "films": len(films),
        "minutes": minutes,
        "days_equiv": round(minutes / 1440, 1),
        "rewatches": sum(f.rewatch_count for f in films),
        "avg_rating": _avg([f.rating for f in films]),
    })

    # attention span
    runtime = _runtime(films)
    if runtime["sample_size"] >= 3 and runtime["longest"]:
        add("runtime", {
            "avg_minutes": runtime["avg_minutes"],
            "longest": runtime["longest"],
            "shortest": runtime["shortest"],
            "share_over_2h": runtime["share_over_2h"],
            "share_under_90m": runtime["share_under_90m"],
        })

    # months
    month_counts = Counter(t.month for t in local_times)
    month_list = [{"month": m, "count": month_counts.get(m, 0)} for m in range(1, 13)]
    elapsed = [m for m in month_list if year < now_local.year or m["month"] <= now_local.month]
    busiest = max(elapsed, key=lambda m: (m["count"], -m["month"]))
    quietest = min(elapsed, key=lambda m: (m["count"], m["month"])) if len(elapsed) >= 2 else None
    _, longest_streak = _streaks([t.date() for t in local_times], now_local.date())
    add("months", {
        "months": month_list,
        "busiest": busiest,
        "quietest": quietest if quietest and quietest is not busiest else None,
        "longest_streak_weeks": longest_streak,
    })

    # genres
    surprise = next((g for g in genres if g["count"] <= 3 and (g["avg_rating"] or 0) >= 4.5 and g is not genres[0]), None)
    if genres:
        add("genres", {"top": genres[:5], "total_genres": len(genres), "surprise": surprise})

    # eras
    if eras["oldest"]:
        add("eras", {
            "decades": _decades(films),
            "oldest": eras["oldest"],
            "newest": eras["newest"],
            "mean_year": eras["mean_release_year"],
        })

    # people
    director = people["directors"]["most_watched"][0] if people["directors"]["most_watched"] else None
    actor = people["actors"]["most_watched"][0] if people["actors"]["most_watched"] else None
    if director or actor:
        add("people", {"director": director, "actor": actor})

    # loves
    loved = sorted(
        (f for f in films if f.rating >= 4),
        key=lambda f: (-f.rating, -f.rewatch_count, -(_delta_vs_world(f) or 0), f.created_at),
    )
    if loved:
        top = loved[0]
        why = "Your highest rating of the year"
        if top.rewatch_count > 0:
            why += f", and you went back {top.rewatch_count} more time{'s' if top.rewatch_count != 1 else ''}"
        elif (_delta_vs_world(top) or 0) >= HOT_TAKE_MIN_DELTA:
            why += ", and you loved it more than the rest of the world did"
        add("loves", {
            "top": [_rated(f) for f in loved[:5]],
            "loved_count": len(loved),
            "film_of_the_year": {**_rated(top), "why": why + "."},
        })

    # hates — always present
    disliked = sorted((f for f in films if f.rating <= 2), key=lambda f: (f.rating, -(f.vote_average or 0), f.created_at))
    add("hates", {
        "worst": [_rated(f) for f in disliked[:3]],
        "disliked_count": len(disliked),
        "one_star_count": sum(1 for f in films if f.rating <= 1),
    })

    # hot take
    takes = vs_world["hot_takes"]["loved_more"] + vs_world["hot_takes"]["loved_less"]
    if takes:
        take = max(takes, key=lambda t: abs(t["delta"]))
        add("hot_take", {**take, "direction": "higher" if take["delta"] > 0 else "lower"})

    # critic
    if vs_world["sample_size"] >= 5:
        world_avg = round(sum((f.vote_average or 0) for f in films if _delta_vs_world(f) is not None) / vs_world["sample_size"] / 2, 2)
        add("critic", {
            "avg_rating": _avg([f.rating for f in films if _delta_vs_world(f) is not None]),
            "world_avg_stars": world_avg,
            "delta": vs_world["mean_delta"],
            "label": vs_world["label"],
            "agreement_share": vs_world["agreement_share"],
        })

    # rewatches (lifetime counts on films first rated this year)
    rewatch = _rewatches(films, limit=3)
    if rewatch["total"] > 0:
        add("rewatches", rewatch)

    # words
    written = [f for f in films if f.words]
    if written:
        longest = max(written, key=lambda f: (f.words, f.created_at))
        excerpt = longest.review_text[:140].rstrip()
        if len(longest.review_text) > 140:
            excerpt += "…"
        add("words", {
            "written_reviews": len(written),
            "words": sum(f.words for f in written),
            "longest": {"movie": _film_ref(longest), "words": longest.words, "excerpt": excerpt, "rating": longest.rating},
        })

    # watchlist
    wl = _watchlist(data, films, now)
    if wl["count"]:
        added_this_year = sum(
            1 for raw in data.watchlist
            if (added := _parse_dt(raw.get("added_at"))) and added.astimezone(tz).year == year
        )
        add("watchlist", {"total": wl["count"], "added_this_year": added_this_year, "oldest": wl["oldest"], "total_minutes": wl["total_minutes"]})

    # friends
    fr = _friends(data, films)
    if fr["twin"]:
        add("friends", {
            "twin": fr["twin"],
            "nemesis": fr["nemesis"],
            "most_disagreed": fr["twin"]["most_disagreed"] if not fr["nemesis"] else fr["nemesis"]["most_disagreed"],
            "compared": len(fr["compared"]),
        })

    # hidden gem + world (need extras coverage)
    extras = _extras(films)
    if extras:
        if extras["hidden_gems"]:
            add("hidden_gem", extras["hidden_gems"][0])
        add("world", {
            "countries": extras["countries"]["count"],
            "top_countries": extras["countries"]["top"][:3],
            "languages": extras["languages"]["count"],
            "non_english_share": extras["languages"]["non_english_share"],
        })

    # persona
    with_year = [f for f in films if f.year]
    night = sum(1 for t in local_times if t.hour in NIGHT_HOURS)
    franchise_counts = Counter(f.collection_id for f in films if f.collection_id is not None)
    metrics = {
        "films": len(films),
        "minutes": minutes,
        "genre_count": len(genres),
        "country_count": extras["countries"]["count"] if extras else None,
        "mean_release_year": eras["mean_release_year"],
        "pre2000_share": _share(sum(1 for f in with_year if f.year < 2000), len(with_year)),
        "recent_share": _share(sum(1 for f in with_year if f.year >= year - 1), len(with_year)),
        "top_genre": genres[0]["name"] if genres else None,
        "top_genre_share": genres[0]["share"] if genres else 0.0,
        "avg_rating": _avg([f.rating for f in films]),
        "disliked_share": _share(len(disliked), len(films)),
        "loved_share": _share(len(loved), len(films)),
        "agreement_share": vs_world["agreement_share"],
        "agreement_sample": vs_world["sample_size"],
        "rewatches": rewatch["total"],
        "max_director_count": director["count"] if director else 0,
        "top_director": director["name"] if director else None,
        "words": sum(f.words for f in films),
        "night_share": _share(night, len(films)),
        "franchises_3plus": sum(1 for n in franchise_counts.values() if n >= 3),
    }
    persona = pick_persona(metrics, year)
    slides.append({"kind": "persona", "theme": persona["theme"], **persona})

    # summary
    summary = {
        "films": len(films),
        "minutes": minutes,
        "avg_rating": metrics["avg_rating"],
        "top_genre": {"id": genres[0]["id"], "name": genres[0]["name"]} if genres else None,
        "top_films": [_rated(f) for f in loved[:5]] if loved else [_rated(f) for f in sorted(films, key=lambda f: -f.rating)[:5]],
        "top_director": director,
        "top_actor": actor,
        "persona": {"key": persona["key"], "title": persona["title"], "emoji": persona["emoji"]},
        "poster_wall": poster_wall,
    }
    slides.append({"kind": "summary", "theme": persona["theme"], **summary})

    return {
        "status": "ready",
        "year": year,
        "generated_at": generated_at,
        "tz": tz_name,
        "films": len(films),
        "minutes": minutes,
        "persona": persona,
        "slides": slides,
        "summary": summary,
    }
