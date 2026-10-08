"""Movies of the Day.

Three films, new every day — the user's own local day, so they turn over at
the user's midnight rather than UTC's. Two kinds of day:

- Most days shuffle the user's For You pool (the ranked feed plus the scored
  overflow behind it), sampled with a bias toward the top: strong matches
  still turn up often, just never the same three.
- Every two or three days, alternating, the picks branch out instead: a genre
  the user loves paired with one they rarely watch (and haven't shown a
  dislike for), sourced straight from TMDB and kept out of For You entirely.
  Nothing in the UI says which kind of day it is; the reason shown is just
  the familiar half of the pairing ("Because you like Animation").

Nothing shown in the last RECENT_DAYS days comes back, and every choice is
seeded on (user, day): reloading doesn't reshuffle, and a slot freed by "not
interested" (or by rating the film) is refilled from the same day's plan on
the next fetch while the other picks stay put.

Which kind of day it is lives in one place, day_kind(), so the planned
day-of-week personalisation can slot in there later.

Storage is the user_weekly_picks table — the name is kept from the weekly
feature this replaced, because renaming it would break the old code mid
deploy. `items` holds today's picks, each stamped with its day and where it
came from; `recent` (sql/010) holds what was shown over the last
RECENT_DAYS days.
"""
from __future__ import annotations

import hashlib
import logging
import random
from datetime import date, datetime, timedelta, timezone, tzinfo

from app.services import streaming_picks, tmdb
from app.services.movie_cache import GENRE_MAP
from app.services.pg import paginate
from app.services.recommendations import (
    UserSignals,
    _backfill_items,
    _hydrate,
    _load_user_signals,
    _upsert_movie_stub,
)
from app.services.supabase_client import get_supabase

logger = logging.getLogger(__name__)

TABLE = "user_weekly_picks"
PICKS_PER_DAY = 3
RECENT_DAYS = 14

# Branch days fall on positions 0 and 2 of a five-day cycle, so they come two
# then three days apart, alternating. Each user's cycle is offset by a hash of
# their id so the whole user base doesn't branch on the same day.
BRANCH_CYCLE = 5
BRANCH_POSITIONS = frozenset({0, 2})

# Shuffle days: the film at rank r of the For You pool is drawn with weight
# SHUFFLE_DECAY ** r. The top of the feed dominates, but because recent picks
# are excluded, the whole pool rotates through over time.
SHUFFLE_DECAY = 0.9

# Branch days.
ANCHOR_POOL = 4  # an anchor genre is drawn from the user's top this-many
MAX_PAIRS = 6  # (anchor, frontier) pairings tried before falling back
BRANCH_MIN_RATING = 6.5
BRANCH_MIN_VOTES = {"vote_average.desc": 250, "popularity.desc": 100}
BRANCH_SORTS = tuple(BRANCH_MIN_VOTES)
BRANCH_MAX_PAGE = 3
BRANCHABLE_GENRES = frozenset(GENRE_MAP) - {10770}  # never TV movies


# ─── The day ─────────────────────────────────────────────────────────────────

def local_day(now: datetime, tz: tzinfo) -> date:
    return now.astimezone(tz).date()


def _phase(user_id: str) -> int:
    # hashlib rather than hash(): Python salts str hashes per process.
    return int(hashlib.sha256(user_id.encode()).hexdigest(), 16) % BRANCH_CYCLE


def day_kind(user_id: str, day: date) -> str:
    """'branch' every two or three days, alternating; otherwise 'shuffle'."""
    return "branch" if (day.toordinal() + _phase(user_id)) % BRANCH_CYCLE in BRANCH_POSITIONS else "shuffle"


def _rng(user_id: str, day: date, salt: str) -> random.Random:
    # random.Random seeded with a str is stable across processes (it hashes
    # the seed with SHA-512), unlike hash()-based seeds.
    return random.Random(f"{user_id}:{day.isoformat()}:{salt}")


# ─── Shuffle days ────────────────────────────────────────────────────────────

def _for_you_pool(supabase, user_id: str) -> list[dict]:
    """The user's For You feed followed by its scored overflow, in rank order.
    Empty if For You has never been computed for them: Movies of the Day
    doesn't trigger that (expensive) computation itself, since the Discover
    page fetches For You alongside it anyway."""
    try:
        result = (
            supabase.table("user_recommendations").select("items, overflow").eq("user_id", user_id).limit(1).execute()
        )
    except Exception:
        logger.exception("Failed to read the For You pool for Movies of the Day")
        return []
    if not result.data:
        return []
    row = result.data[0]
    seen: set[int] = set()
    pool: list[dict] = []
    for it in (row.get("items") or []) + (row.get("overflow") or []):
        mid = it.get("movie_id")
        if mid is None or mid in seen:
            continue
        seen.add(mid)
        pool.append(it)
    return pool


def pick_from_pool(pool: list[dict], exclude: set[int], n: int, rng: random.Random) -> list[dict]:
    """n films from the For You pool by weighted sampling without replacement
    (Efraimidis–Spirakis: each film's key is u ** (1 / weight) and the
    highest keys win). Keys are drawn for every film before exclusions are
    applied, so a film's chances don't shift when others are excluded."""
    keyed = [(rng.random() ** (1 / (SHUFFLE_DECAY ** rank)), it) for rank, it in enumerate(pool)]
    keyed.sort(key=lambda t: t[0], reverse=True)
    picks = [it for _, it in keyed if it["movie_id"] not in exclude][:n]
    return [{"movie_id": it["movie_id"], "reason": it.get("reason") or "Picked for you", "source": "shuffle"} for it in picks]


# ─── Branch days ─────────────────────────────────────────────────────────────

def branch_pairs(signals: UserSignals, rng: random.Random) -> list[tuple[int, int]]:
    """(anchor, frontier) genre pairings to try, in order.

    The anchor is one of the user's favourite genres, drawn in proportion to
    how much they like it. The frontier is a genre outside their favourites
    that they haven't rated down more than up. Films are then required to be
    both, so every pick shares some of the user's taste but not all of it.
    Pairings that match a "not interested in these types" dismissal are
    skipped."""
    affinity = {g: w for g, w in signals.genre_affinity.items() if g in BRANCHABLE_GENRES and w > 0}
    if not affinity:
        return []
    anchors = sorted(affinity, key=lambda g: (-affinity[g], g))[:ANCHOR_POOL]
    frontier = sorted(
        g for g in BRANCHABLE_GENRES
        if g not in anchors and signals.genre_penalty.get(g, 0) <= affinity.get(g, 0)
    )
    if not frontier:
        return []

    pairs: list[tuple[int, int]] = []
    for _ in range(MAX_PAIRS * 4):
        if len(pairs) >= MAX_PAIRS:
            break
        anchor = rng.choices(anchors, weights=[affinity[g] for g in anchors])[0]
        pair = (anchor, rng.choice(frontier))
        if pair in pairs or any(set(pair) <= d["genre_ids"] for d in signals.type_dislikes):
            continue
        pairs.append(pair)
    return pairs


def _discover_pair(anchor: int, other: int, rng: random.Random) -> list[dict]:
    """Well-rated films that are both genres, from a random one of the first
    few result pages and in a shuffled order, so each branch day reaches
    further than the obvious top results."""
    sort_by = rng.choice(BRANCH_SORTS)
    params = {
        "with_genres": f"{anchor},{other}",  # comma = AND on TMDB
        "without_genres": "10770",
        "sort_by": sort_by,
        "vote_count.gte": BRANCH_MIN_VOTES[sort_by],
        "vote_average.gte": BRANCH_MIN_RATING,
        "include_adult": "false",
    }
    page = rng.randint(1, BRANCH_MAX_PAGE)
    results = tmdb.discover_movies({**params, "page": page}).get("results") or []
    if len(results) < PICKS_PER_DAY and page > 1:
        results = tmdb.discover_movies({**params, "page": 1}).get("results") or []
    results = [m for m in results if m.get("id") and m.get("title") and m.get("poster_path")]
    rng.shuffle(results)
    return results


def pick_branch(signals: UserSignals, exclude: set[int], n: int, rng: random.Random, supabase) -> list[dict]:
    """n films from a branch-out pairing, taken from one pairing where
    possible so a branch day reads as a single theme."""
    picked: list[dict] = []
    stubs: list[dict] = []
    excluded = set(exclude)
    for anchor, other in branch_pairs(signals, rng):
        if len(picked) >= n:
            break
        try:
            films = _discover_pair(anchor, other, rng)
        except Exception:
            logger.exception("TMDB discover failed for Movies of the Day pairing %s/%s", anchor, other)
            continue
        for m in films:
            if len(picked) >= n:
                break
            if m["id"] in excluded:
                continue
            excluded.add(m["id"])
            picked.append({"movie_id": m["id"], "reason": f"Because you like {GENRE_MAP[anchor]}", "source": "branch"})
            stubs.append(_upsert_movie_stub(m))

    # Branch films often aren't cached yet, and _hydrate only reads the movies
    # table. Insert-only, like every other stub write.
    if stubs:
        try:
            supabase.table("movies").upsert(stubs, on_conflict="id", ignore_duplicates=True).execute()
        except Exception:
            logger.exception("Failed to insert Movies of the Day stubs")
    return picked


# ─── Planning a day ──────────────────────────────────────────────────────────

def _excluded_ids(supabase, user_id: str) -> set[int]:
    """Films never to offer: rated, on the watchlist, or dismissed."""
    ids: set[int] = set()
    for table in ("reviews", "watchlist", "dismissed_recommendations"):
        rows = paginate(lambda t=table: supabase.table(t).select("movie_id").eq("user_id", user_id).order("movie_id"))
        ids |= {r["movie_id"] for r in rows}
    return ids


def plan(user_id: str, day: date, supabase, n: int, shown: set[int]) -> list[dict]:
    """n new picks for `day`, never repeating anything in `shown`."""
    kind = day_kind(user_id, day)
    exclude = _excluded_ids(supabase, user_id) | shown
    pool = _for_you_pool(supabase, user_id)
    signals: UserSignals | None = None

    def branch(avoid: set[int], k: int) -> list[dict]:
        # The taste signals enrich every film the user has rated, so they are
        # only loaded on the days that branch.
        nonlocal signals
        if signals is None:
            signals = _load_user_signals(user_id, supabase)
        return pick_branch(signals, avoid, k, _rng(user_id, day, "branch"), supabase)

    def taken(picks: list[dict]) -> set[int]:
        return exclude | {p["movie_id"] for p in picks}

    picked: list[dict] = []
    if kind == "branch":
        # Kept out of For You entirely: the point is something they wouldn't
        # otherwise be shown.
        picked = branch(exclude | {it["movie_id"] for it in pool}, n)
    if len(picked) < n:
        picked += pick_from_pool(pool, taken(picked), n - len(picked), _rng(user_id, day, "shuffle"))
    if len(picked) < n and kind == "shuffle":
        # No For You pool yet (or it's exhausted): branch out instead of
        # falling straight back to generic top-rated films.
        picked += branch(taken(picked), n - len(picked))
    if len(picked) < n:
        backfill = _backfill_items(taken(picked), n - len(picked), supabase)
        picked += [{**it, "source": "backfill"} for it in backfill]

    return [{**p, "day": day.isoformat()} for p in picked[:n]]


# ─── Storage ─────────────────────────────────────────────────────────────────

def _load_row(supabase, user_id: str) -> dict | None:
    try:
        result = supabase.table(TABLE).select("items, computed_at, recent").eq("user_id", user_id).limit(1).execute()
    except Exception as exc:
        if "recent" not in str(exc):
            raise
        # sql/010 not applied yet.
        result = supabase.table(TABLE).select("items, computed_at").eq("user_id", user_id).limit(1).execute()
    return result.data[0] if result.data else None


def _save_row(supabase, user_id: str, items: list[dict], recent: list[dict], now: datetime) -> None:
    payload = {"user_id": user_id, "items": items, "recent": recent, "computed_at": now.isoformat()}
    try:
        supabase.table(TABLE).upsert(payload, on_conflict="user_id").execute()
    except Exception as exc:
        if "recent" not in str(exc):
            raise
        logger.error("user_weekly_picks has no `recent` column - apply sql/010. Saving without the history.")
        payload.pop("recent")
        supabase.table(TABLE).upsert(payload, on_conflict="user_id").execute()


def history(row: dict | None, today: date) -> list[dict]:
    """Everything shown in the last RECENT_DAYS days: the stored history plus
    whatever the row currently holds (which also covers rows written by the
    weekly feature, and installs that haven't applied sql/010 yet)."""
    if not row:
        return []
    entries = list(row.get("recent") or [])
    for it in row.get("items") or []:
        entries.append({"movie_id": it.get("movie_id"), "day": it.get("day") or today.isoformat()})

    cutoff = today - timedelta(days=RECENT_DAYS)
    kept: dict[int, dict] = {}
    for e in entries:
        try:
            shown_on = date.fromisoformat(e["day"])
            movie_id = int(e["movie_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if shown_on > cutoff and movie_id not in kept:
            kept[movie_id] = {"movie_id": movie_id, "day": shown_on.isoformat()}
    return list(kept.values())


# ─── Public API ──────────────────────────────────────────────────────────────

def get_daily_picks(user_id: str, tz: tzinfo, force: bool = False, now: datetime | None = None) -> dict:
    """Today's three, hydrated. Computed on the first fetch of the user's day,
    topped up if a pick was taken out since, otherwise straight from the row."""
    supabase = get_supabase()
    now = now or datetime.now(timezone.utc)
    today = local_day(now, tz)
    row = _load_row(supabase, user_id)

    todays = [] if force else [it for it in (row or {}).get("items") or [] if it.get("day") == today.isoformat()]
    if len(todays) < PICKS_PER_DAY:
        past = history(row, today)
        shown = {e["movie_id"] for e in past} | {it["movie_id"] for it in todays}
        new = plan(user_id, today, supabase, PICKS_PER_DAY - len(todays), shown)
        todays = todays + new
        recent = past + [{"movie_id": it["movie_id"], "day": today.isoformat()} for it in new]
        _save_row(supabase, user_id, todays, recent, now)
    return _hydrate(todays, supabase)


# How many drop-and-refill rounds to try before settling for however many
# picks ended up available — each round can replace several slots in one
# get_daily_picks() call, so this bounds worst case to a handful of rounds
# even with a very narrow provider selection, not an unbounded loop.
_STREAMING_BACKFILL_MAX_ROUNDS = 4


def get_daily_picks_streaming(user_id: str, tz: tzinfo, provider_ids: list[int], force: bool = False) -> dict:
    """Today's picks, filtered to the user's streaming services *without
    shrinking the count* — a pick that doesn't pass the filter is dropped
    the same way "not interested" drops one (daily_picks.drop_from_daily_picks),
    which is already designed to have the next fetch refill just that slot;
    this just repeats that drop-and-refetch a few times, filtering again
    each round, until every slot passes or there's nothing better to offer."""
    data = get_daily_picks(user_id, tz, force=force)
    if not provider_ids:
        return data

    supabase = get_supabase()
    for _ in range(_STREAMING_BACKFILL_MAX_ROUNDS):
        kept = streaming_picks.filter_by_availability(data["results"], provider_ids)
        if len(kept) >= len(data["results"]):
            data["results"] = kept
            data["total_results"] = len(kept)
            return data
        unavailable_ids = {m["id"] for m in data["results"]} - {m["id"] for m in kept}
        for movie_id in unavailable_ids:
            drop_from_daily_picks(supabase, user_id, movie_id)
        data = get_daily_picks(user_id, tz)

    data["results"] = streaming_picks.filter_by_availability(data["results"], provider_ids)
    data["total_results"] = len(data["results"])
    return data


def drop_from_daily_picks(supabase, user_id: str, movie_id: int) -> bool:
    """Take a film out of today's picks after a "not interested" or a rating.
    The other picks stay; the freed slot is refilled on the next fetch from
    the same day's plan. The history keeps the film so it can't come back.
    Returns whether it was one of the picks."""
    row = _load_row(supabase, user_id)
    items = (row or {}).get("items") or []
    if not any(it.get("movie_id") == movie_id for it in items):
        return False
    remaining = [it for it in items if it.get("movie_id") != movie_id]
    supabase.table(TABLE).update({"items": remaining}).eq("user_id", user_id).execute()
    return True
