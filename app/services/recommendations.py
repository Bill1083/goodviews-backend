import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from flask import current_app

from app.services import movie_cache, tmdb
from app.services.supabase_client import get_supabase

logger = logging.getLogger(__name__)

CACHE_TTL = timedelta(hours=24)
FEED_SIZE = 24
MAX_FRIEND_SHARE = 0.15  # friend signal is a light sprinkle, not a major share (confirmed: 10-20% of the feed)
MAX_GENRE_SHARE = 0.4
MAX_PICKS_PER_PERSON = 2
DIVERSITY_SLOTS = 3  # reserved for 3-star-seeded ("diversity") picks
MAX_CANDIDATES_TO_ENRICH = 120

# How much of the feed a single rated movie is allowed to seed, scaling with
# how many movies the user has actually rated — one 5★ review shouldn't
# dominate the whole feed. At 10+ movies rated ≥4★ (or an equivalent mix
# with 3★s at half weight), review-based signal can fill the entire feed;
# with fewer, the rest is topped up with generic popular-movie backfill.
REVIEW_SHARE_PER_POSITIVE = 0.10
REVIEW_SHARE_PER_DIVERSITY = 0.05

# Positive weight by rating bucket (5 > 4 > 3-diversity); negative weight
# (magnitude, applied as a penalty) by rating bucket (1 > 2).
_POSITIVE_WEIGHT = {5: 2.0, 4: 1.0, 3: 0.4}
_NEGATIVE_WEIGHT = {1: 2.0, 2: 1.0}
_REWATCH_BONUS = 0.5
_FAVOURITE_BONUS = 3.0
_ONBOARDING_GENRE_BONUS = 1.5
_FRIEND_BOOST_PER_FRIEND = 2.0
_PROVENANCE_BONUS = {"seed_rec": 0.5, "favourite": 0.3, "friend": 0.4, "diversity": 0.1}
_VOTE_AVERAGE_WEIGHT = 0.1


def get_recommendations_for_user(user_id: str, force: bool = False) -> dict:
    """Personalized 'For You' feed, cached in user_recommendations for 24h.
    Returns the same shape as a TMDB movie-list response so the frontend can
    treat it exactly like /trending or /top-rated."""
    supabase = get_supabase()
    row = None
    if not force:
        cached = (
            supabase.table("user_recommendations")
            .select("items, computed_at")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        row = cached.data[0] if cached.data else None

    if row and _is_fresh(row["computed_at"]):
        return _hydrate(row["items"], supabase)

    items = _compute(user_id, supabase)
    supabase.table("user_recommendations").upsert(
        {"user_id": user_id, "items": items, "computed_at": datetime.now(timezone.utc).isoformat()},
        on_conflict="user_id",
    ).execute()
    return _hydrate(items, supabase)


def _is_fresh(computed_at: str) -> bool:
    ts = datetime.fromisoformat(computed_at.replace("Z", "+00:00"))
    return datetime.now(timezone.utc) - ts < CACHE_TTL


def _hydrate(items: list[dict], supabase) -> dict:
    """Turns cached [{movie_id, reason}] rank order back into full movie dicts."""
    if not items:
        return {"page": 1, "results": [], "total_pages": 1, "total_results": 0}

    reason_by_id = {it["movie_id"]: it["reason"] for it in items}
    result = (
        supabase.table("movies")
        .select("id, title, poster_path, backdrop_path, release_date, vote_average, genre_ids")
        .in_("id", list(reason_by_id.keys()))
        .execute()
    )
    movie_by_id = {m["id"]: m for m in result.data}

    ordered = []
    for it in items:
        m = movie_by_id.get(it["movie_id"])
        if not m:
            continue
        entry = dict(m)
        entry["reason"] = reason_by_id[it["movie_id"]]
        ordered.append(entry)
    return {"page": 1, "results": ordered, "total_pages": 1, "total_results": len(ordered)}


def _fetch_credits(movie_ids: list[int]) -> dict[int, dict]:
    """Parallel-enriches a batch of movie ids with title/genre_ids/vote_average
    plus the top-5 billed cast ids + director ids, via the segmented cache —
    same ThreadPoolExecutor pattern as reviews.py's _enrich_movies.

    Each worker thread pushes its own Flask app context: movie_cache.get_movie
    needs current_app (TTL config, TMDB API key), which isn't available in a
    thread the request context wasn't pushed into by default."""
    if not movie_ids:
        return {}

    app = current_app._get_current_object()

    def _one(mid: int):
        with app.app_context():
            try:
                data = movie_cache.get_movie(mid, segments=("core",))
            except Exception:
                return mid, None
        credits = data.get("credits") or {}
        cast_ids = [c["id"] for c in (credits.get("cast") or [])[:5] if c.get("id")]
        director_ids = [c["id"] for c in (credits.get("crew") or []) if c.get("job") == "Director" and c.get("id")]
        return mid, {
            "title": data.get("title"),
            "poster_path": data.get("poster_path"),
            "release_date": data.get("release_date"),
            "genre_ids": data.get("genre_ids") or [],
            "person_ids": cast_ids + director_ids,
            "vote_average": data.get("vote_average") or 0,
        }

    out: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=min(len(movie_ids), 10)) as executor:
        futures = [executor.submit(_one, mid) for mid in movie_ids]
        for future in as_completed(futures):
            mid, info = future.result()
            if info:
                out[mid] = info
    return out


def _upsert_movie_stub(m: dict) -> dict:
    """Lightweight upsert shape for a TMDB-sourced candidate not already
    cached — same fallback-stub fields reviews.py's create_review uses."""
    return {
        "id": m["id"],
        "title": m.get("title"),
        "poster_path": m.get("poster_path"),
        "backdrop_path": m.get("backdrop_path"),
        "release_date": m.get("release_date"),
        "vote_average": m.get("vote_average") or None,
        "genre_ids": m.get("genre_ids") or [],
    }


def _backfill_items(exclude_ids: set[int], limit: int, supabase) -> list[dict]:
    """Cold-start / not-enough-candidates safety net: TMDB's global top-rated
    list, minus anything already excluded. Persists stubs so _hydrate can
    find these movies afterward (browsing the plain Top Rated tab never
    writes into the movies table, unlike viewing/reviewing a movie)."""
    items: list[dict] = []
    stubs: list[dict] = []
    seen: set[int] = set()
    page = 1
    while len(items) < limit and page <= 3:
        try:
            data = tmdb.get_top_rated_movies(page)
        except Exception:
            break
        for m in data.get("results", []):
            mid = m.get("id")
            if not mid or mid in exclude_ids or mid in seen:
                continue
            seen.add(mid)
            stubs.append(_upsert_movie_stub(m))
            items.append({"movie_id": mid, "reason": "Popular right now"})
            if len(items) >= limit:
                break
        page += 1

    if stubs:
        try:
            supabase.table("movies").upsert(stubs, on_conflict="id").execute()
        except Exception:
            logger.exception("Failed to upsert backfill movie stubs")
    return items


def _reason_for(provenance: str, top_contributor: tuple[str, str] | None) -> str:
    if provenance == "friend" and top_contributor:
        return f"{top_contributor[1]} rated this highly"
    if top_contributor and top_contributor[0] == "seed":
        return f"Because you liked {top_contributor[1]}"
    if top_contributor and top_contributor[0] == "person":
        return f"Because you like {top_contributor[1]} movies"
    return "Popular right now"


def _compute(user_id: str, supabase) -> list[dict]:
    app = current_app._get_current_object()
    my_reviews = (
        supabase.table("reviews").select("movie_id, rating, rewatch_count").eq("user_id", user_id).execute().data
    )
    reviewed_ids = {r["movie_id"] for r in my_reviews}
    watchlist_ids = {
        r["movie_id"] for r in supabase.table("watchlist").select("movie_id").eq("user_id", user_id).execute().data
    }
    excluded_ids = reviewed_ids | watchlist_ids

    fav_actors = (
        supabase.table("favorite_actors").select("actor_id, actor_name").eq("user_id", user_id).execute().data
    )
    fav_directors = (
        supabase.table("favorite_directors")
        .select("director_id, director_name")
        .eq("user_id", user_id)
        .execute()
        .data
    )
    profile_rows = supabase.table("profiles").select("onboarding_genre_ids").eq("id", user_id).limit(1).execute().data
    onboarding_genre_ids = (profile_rows[0].get("onboarding_genre_ids") or []) if profile_rows else []

    positive_seeds = [r for r in my_reviews if r["rating"] >= 4]
    diversity_seeds = [r for r in my_reviews if r["rating"] == 3]
    negative_seeds = [r for r in my_reviews if r["rating"] <= 2]

    if not (positive_seeds or diversity_seeds or fav_actors or fav_directors or onboarding_genre_ids):
        # Cold start: nothing to personalize on yet (shouldn't normally happen
        # once onboarding is complete, but is the safety net if it isn't).
        return _backfill_items(excluded_ids, FEED_SIZE, supabase)

    # ── Build affinity from the user's own ratings + explicit favourites ──
    seed_ids = list({r["movie_id"] for r in positive_seeds + diversity_seeds + negative_seeds})
    seed_info = _fetch_credits(seed_ids)

    genre_affinity: dict[int, float] = {}
    person_affinity: dict[int, float] = {}
    genre_penalty: dict[int, float] = {}
    person_penalty: dict[int, float] = {}

    def _weight(rating: float, rewatch_count: int) -> float:
        bucket = 5 if rating >= 4.5 else (4 if rating >= 4 else 3)
        w = _POSITIVE_WEIGHT[bucket]
        if rewatch_count:
            w += _REWATCH_BONUS
        return w

    for r in positive_seeds + diversity_seeds:
        info = seed_info.get(r["movie_id"])
        if not info:
            continue
        w = _weight(r["rating"], r.get("rewatch_count") or 0)
        for gid in info["genre_ids"]:
            genre_affinity[gid] = genre_affinity.get(gid, 0) + w
        for pid in info["person_ids"]:
            person_affinity[pid] = person_affinity.get(pid, 0) + w

    for r in negative_seeds:
        info = seed_info.get(r["movie_id"])
        if not info:
            continue
        w = _NEGATIVE_WEIGHT[1] if r["rating"] <= 1 else _NEGATIVE_WEIGHT[2]
        for gid in info["genre_ids"]:
            genre_penalty[gid] = genre_penalty.get(gid, 0) + w
        for pid in info["person_ids"]:
            person_penalty[pid] = person_penalty.get(pid, 0) + w

    for gid in onboarding_genre_ids:
        genre_affinity[gid] = genre_affinity.get(gid, 0) + _ONBOARDING_GENRE_BONUS
    for row in fav_actors:
        person_affinity[row["actor_id"]] = person_affinity.get(row["actor_id"], 0) + _FAVOURITE_BONUS
    for row in fav_directors:
        person_affinity[row["director_id"]] = person_affinity.get(row["director_id"], 0) + _FAVOURITE_BONUS

    # ── Candidate generation, tagged with provenance + a "top contributor"
    #    used only for the reason string (which specific seed/actor/friend
    #    this candidate came from) — kept separate from scoring, which uses
    #    the real genre/person affinity maps above. ─────────────────────────
    candidates: dict[int, dict] = {}

    def _add(results: list[dict], provenance: str, top_contributor: tuple[str, str]):
        for m in results:
            mid = m.get("id")
            if not mid or mid in excluded_ids or mid in seed_ids or mid in candidates:
                continue
            candidates[mid] = {
                "provenance": provenance,
                "top_contributor": top_contributor,
                "title": m.get("title"),
                "poster_path": m.get("poster_path"),
                "backdrop_path": m.get("backdrop_path"),
                "release_date": m.get("release_date"),
                "vote_average": m.get("vote_average") or 0,
                "genre_ids": m.get("genre_ids") or [],
                "person_ids": [],
            }

    # Each of these is its own TMDB call (with its own internal retry/backoff
    # on connection resets) — running them one at a time could add up to a
    # minute-plus of sequential wait under any TMDB flakiness. Fan them out
    # in parallel instead so total latency is ~the slowest single call per
    # batch, not the sum of all of them.
    top_positive = sorted(positive_seeds, key=lambda r: (r["rating"], r.get("rewatch_count") or 0), reverse=True)[:8]
    tasks: list[tuple[str, int, str]] = []
    for r in top_positive:
        info = seed_info.get(r["movie_id"])
        title = info["title"] if info and info.get("title") else "a movie you rated"
        tasks.append(("seed_rec", r["movie_id"], title))
    for row in fav_actors[:5]:
        tasks.append(("favourite_actor", row["actor_id"], row["actor_name"]))
    for row in fav_directors[:5]:
        tasks.append(("favourite_director", row["director_id"], row["director_name"]))
    for r in diversity_seeds[:2]:
        info = seed_info.get(r["movie_id"])
        title = info["title"] if info and info.get("title") else "a movie you rated"
        tasks.append(("diversity", r["movie_id"], title))
    # Onboarding genre picks are an explicit preference signal like
    # favourites, not tied to review count — a user who only did step 1
    # still gets some personalization instead of pure backfill.
    for gid in onboarding_genre_ids[:3]:
        genre_name = movie_cache.GENRE_MAP.get(gid, "that genre")
        tasks.append(("favourite_genre", gid, genre_name))

    def _run_task(task: tuple[str, int, str]):
        kind, key, label = task
        with app.app_context():
            try:
                if kind in ("seed_rec", "diversity"):
                    data = tmdb.get_movie_recommendations(key)
                    if kind == "seed_rec" and len(data.get("results", [])) < 5:
                        data = tmdb.get_similar_movies(key)
                elif kind == "favourite_actor":
                    data = tmdb.discover_movies(
                        {"with_cast": key, "sort_by": "vote_average.desc", "vote_count.gte": 100}
                    )
                elif kind == "favourite_director":
                    data = tmdb.discover_movies(
                        {"with_crew": key, "sort_by": "vote_average.desc", "vote_count.gte": 100}
                    )
                else:
                    data = tmdb.discover_movies(
                        {"with_genres": key, "sort_by": "popularity.desc", "vote_count.gte": 100}
                    )
            except Exception:
                data = {}
        provenance = "favourite" if kind.startswith("favourite") else kind
        contributor_type = "seed" if kind in ("seed_rec", "diversity") else "person"
        return provenance, (contributor_type, label), data.get("results", [])

    if tasks:
        # executor.map preserves task order in its output (unlike as_completed),
        # so applying results in this order keeps _add's first-writer-wins
        # dedup priority the same as the old sequential version: seed_rec,
        # then favourites, then diversity.
        with ThreadPoolExecutor(max_workers=min(len(tasks), 10)) as executor:
            for provenance, top_contributor, results in executor.map(_run_task, tasks):
                _add(results, provenance, top_contributor)

    # ── Friend signal: a friend's 4-5★ review boosts a movie; a friend's
    #    1-2★ review vetoes it outright, regardless of every other signal. ──
    friend_ids = [r["friend_id"] for r in supabase.table("friendships").select("friend_id").eq("user_id", user_id).execute().data]
    friend_positive: dict[int, list[tuple[str, float]]] = {}
    veto_ids: set[int] = set()
    if friend_ids:
        friend_reviews = (
            supabase.table("reviews")
            .select("movie_id, rating, user_id")
            .in_("user_id", friend_ids)
            .execute()
            .data
        )
        for r in friend_reviews:
            if r["rating"] >= 4:
                friend_positive.setdefault(r["movie_id"], []).append((r["user_id"], r["rating"]))
            elif r["rating"] <= 2:
                veto_ids.add(r["movie_id"])

        new_friend_ids = [
            mid for mid in friend_positive if mid not in excluded_ids and mid not in seed_ids and mid not in candidates
        ]
        for mid in new_friend_ids:
            candidates[mid] = {
                "provenance": "friend",
                "top_contributor": None,
                "title": None,
                "poster_path": None,
                "backdrop_path": None,
                "release_date": None,
                "vote_average": 0,
                "genre_ids": [],
                "person_ids": [],
            }

        top_friend_ids = {fid for votes in friend_positive.values() for fid, _ in votes}
        friend_usernames: dict[str, str] = {}
        if top_friend_ids:
            profs = supabase.table("profiles").select("id, username").in_("id", list(top_friend_ids)).execute().data
            friend_usernames = {p["id"]: p["username"] for p in profs}
        for mid, c in candidates.items():
            if c["provenance"] == "friend" and mid in friend_positive:
                best_friend_id, _ = max(friend_positive[mid], key=lambda t: t[1])
                c["top_contributor"] = ("friend", friend_usernames.get(best_friend_id, "A friend"))

    for mid in veto_ids:
        candidates.pop(mid, None)

    if not candidates:
        return _backfill_items(excluded_ids, FEED_SIZE, supabase)

    # ── Enrich candidates with full credits (genre_ids + person_ids), capped
    #    to bound worst-case latency on a cold cache. Friend candidates are
    #    always enriched (there are few of them); the remaining budget goes
    #    to the highest vote_average candidates from TMDB. ──────────────────
    friend_mids = [mid for mid, c in candidates.items() if c["provenance"] == "friend"]
    other_mids_sorted = sorted(
        (mid for mid in candidates if candidates[mid]["provenance"] != "friend"),
        key=lambda mid: candidates[mid]["vote_average"],
        reverse=True,
    )
    remaining_budget = max(0, MAX_CANDIDATES_TO_ENRICH - len(friend_mids))
    enrich_order = friend_mids + other_mids_sorted[:remaining_budget]
    enrich_info = _fetch_credits(enrich_order)
    for mid, info in enrich_info.items():
        c = candidates[mid]
        c["title"] = c["title"] or info["title"]
        # Backfill from the authoritative TMDB details fetch whenever the
        # candidate-generation source (a TMDB list item, or a bare friend-
        # rated movie_id with no list data at all) didn't carry these —
        # never overwrite a value that's already present.
        c["poster_path"] = c["poster_path"] or info["poster_path"]
        c["release_date"] = c["release_date"] or info["release_date"]
        c["genre_ids"] = info["genre_ids"] or c["genre_ids"]
        c["person_ids"] = info["person_ids"]
        if not c["vote_average"]:
            c["vote_average"] = info["vote_average"]

    # Drop anything we couldn't enrich AND has no genre data to score on
    # (only possible for friend candidates whose movie_cache fetch failed).
    candidates = {mid: c for mid, c in candidates.items() if mid in enrich_info or c["genre_ids"]}
    if not candidates:
        return _backfill_items(excluded_ids, FEED_SIZE, supabase)

    # ── Score ────────────────────────────────────────────────────────────
    scored: list[tuple[int, float, dict]] = []
    for mid, c in candidates.items():
        score = _PROVENANCE_BONUS.get(c["provenance"], 0) + _VOTE_AVERAGE_WEIGHT * (c["vote_average"] or 0)
        for gid in c["genre_ids"]:
            score += genre_affinity.get(gid, 0) - genre_penalty.get(gid, 0)
        for pid in c["person_ids"]:
            score += person_affinity.get(pid, 0) - person_penalty.get(pid, 0)
        # Applies whenever a friend rated this movie highly, regardless of
        # provenance — a movie already found via seed_rec/favourite that a
        # friend also loved should get credit for that too, not just the
        # candidates discovered purely through friend signal.
        votes = friend_positive.get(mid, [])
        if votes:
            avg_friend_rating = sum(v for _, v in votes) / len(votes)
            score += _FRIEND_BOOST_PER_FRIEND * len(votes) + 0.5 * (avg_friend_rating - 3)
        scored.append((mid, score, c))

    # Dropping anything scoring <=0 is the concrete "1-2★ signal deprioritizes
    # /excludes similar candidates" behavior, not just an omission of positive credit.
    scored = [t for t in scored if t[1] > 0]
    scored.sort(key=lambda t: t[1], reverse=True)

    # ── Diversify: cap any one actor/director/genre's share of the final
    #    list, cap friend-provenance share to ~15%, and reserve a few slots
    #    specifically for "diversity" (3★-seeded) picks. ────────────────────
    selected: list[tuple[int, dict]] = []
    genre_counts: dict[int, int] = {}
    person_counts: dict[int, int] = {}
    friend_count = 0
    review_based_count = 0
    max_friend = round(FEED_SIZE * MAX_FRIEND_SHARE)
    max_per_genre = max(1, round(FEED_SIZE * MAX_GENRE_SHARE))
    review_share = min(1.0, REVIEW_SHARE_PER_POSITIVE * len(positive_seeds) + REVIEW_SHARE_PER_DIVERSITY * len(diversity_seeds))
    max_review_based = round(FEED_SIZE * review_share)

    def _try_add(mid: int, c: dict) -> bool:
        nonlocal friend_count, review_based_count
        if c["provenance"] == "friend" and friend_count >= max_friend:
            return False
        if c["provenance"] in ("seed_rec", "diversity") and review_based_count >= max_review_based:
            return False
        if any(person_counts.get(pid, 0) >= MAX_PICKS_PER_PERSON for pid in c["person_ids"]):
            return False
        if any(genre_counts.get(gid, 0) >= max_per_genre for gid in c["genre_ids"]):
            return False
        selected.append((mid, c))
        for pid in c["person_ids"]:
            person_counts[pid] = person_counts.get(pid, 0) + 1
        for gid in c["genre_ids"]:
            genre_counts[gid] = genre_counts.get(gid, 0) + 1
        if c["provenance"] == "friend":
            friend_count += 1
        if c["provenance"] in ("seed_rec", "diversity"):
            review_based_count += 1
        return True

    main_pool = [t for t in scored if t[2]["provenance"] != "diversity"]
    diversity_pool = [t for t in scored if t[2]["provenance"] == "diversity"]

    for mid, _, c in main_pool:
        if len(selected) >= FEED_SIZE - DIVERSITY_SLOTS:
            break
        _try_add(mid, c)

    for mid, _, c in diversity_pool:
        if len(selected) >= FEED_SIZE:
            break
        _try_add(mid, c)

    if len(selected) < FEED_SIZE:
        chosen_ids = {mid for mid, _ in selected}
        for mid, _, c in main_pool:
            if len(selected) >= FEED_SIZE:
                break
            if mid not in chosen_ids:
                _try_add(mid, c)

    # ── Persist stubs for anything not already cached ──────────────────────
    stubs = [_upsert_movie_stub({"id": mid, **c}) for mid, c in selected if c.get("title")]
    if stubs:
        try:
            supabase.table("movies").upsert(stubs, on_conflict="id").execute()
        except Exception:
            logger.exception("Failed to upsert recommendation movie stubs")

    items = [{"movie_id": mid, "reason": _reason_for(c["provenance"], c["top_contributor"])} for mid, c in selected]

    # Top up to FEED_SIZE with generic popular-movie backfill — always, not
    # just when things are sparse, since the review-based proportional cap
    # above deliberately leaves this gap for anyone without much review
    # history yet (including a fully skipped onboarding).
    if len(items) < FEED_SIZE:
        chosen_ids = {mid for mid, _ in selected}
        items += _backfill_items(excluded_ids | chosen_ids, FEED_SIZE - len(items), supabase)

    return items
