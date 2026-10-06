"""Movies of the Day (app/services/daily_picks.py): the rhythm of the days,
the two ways a day's films are chosen, and what is stored between fetches.
PostgREST and TMDB are faked; the user's taste signals are injected."""
import random
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app import create_app
from app.controllers import movies as movies_controller
from app.services import daily_picks
from app.services.recommendations import UserSignals
from app.utils import auth as auth_utils
from tests.fakes import FakeSupabase

USER = "user-1"
UTC = ZoneInfo("UTC")
START = date(2026, 10, 6)


# ─── Builders ────────────────────────────────────────────────────────────────

def signals(affinity=None, penalty=None, type_dislikes=(), reviewed=(), watchlist=(), dismissed=()) -> UserSignals:
    return UserSignals(
        reviewed_ids=set(reviewed), watchlist_ids=set(watchlist), dismissed_ids=set(dismissed),
        positive_seeds=[], diversity_seeds=[], negative_seeds=[], seed_ids=[], seed_info={},
        fav_actors=[], fav_directors=[], onboarding_genre_ids=[],
        genre_affinity=dict(affinity or {}), person_affinity={},
        genre_penalty=dict(penalty or {}), person_penalty={},
        type_dislikes=[{"genre_ids": set(g), "director_ids": set(), "age_days": 1.0} for g in type_dislikes],
    )


def tmdb_film(mid: int, genres=(18,), poster=True) -> dict:
    return {
        "id": mid, "title": f"Film {mid}", "poster_path": f"/p{mid}.jpg" if poster else None,
        "backdrop_path": f"/b{mid}.jpg", "release_date": "2015-05-05", "vote_average": 7.5,
        "genre_ids": list(genres),
    }


def movie_row(mid: int) -> dict:
    film = tmdb_film(mid)
    return {k: film[k] for k in ("id", "title", "poster_path", "backdrop_path", "release_date", "vote_average", "genre_ids")}


class FakeDiscover:
    """TMDB /discover/movie: six good films per genre pairing plus one with
    no poster (which must never be picked) — the same ones on every call."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, params: dict) -> dict:
        self.calls.append(dict(params))
        a, b = sorted(int(g) for g in params["with_genres"].split(","))
        base = 10_000_000 + (a * 100_000 + b) * 10
        films = [tmdb_film(base + i, (a, b)) for i in range(6)]
        films.append(tmdb_film(base + 9, (a, b), poster=False))
        return {"results": films}


def pool_items(pool_ids) -> list[dict]:
    return [{"movie_id": m, "reason": f"Because you liked Film {m}"} for m in pool_ids]


def setup(monkeypatch, pool_ids=range(100, 130), affinity=None, row=None, missing_columns=None,
          reviewed=(), watchlist=(), dismissed=(), **signal_kw):
    pool = pool_items(pool_ids)
    db = FakeSupabase({
        "user_recommendations": [{"user_id": USER, "items": pool[:10], "overflow": pool[10:]}] if pool else [],
        "movies": [movie_row(m) for m in pool_ids],
        "user_weekly_picks": [row] if row else [],
        "reviews": [{"user_id": USER, "movie_id": m} for m in reviewed],
        "watchlist": [{"user_id": USER, "movie_id": m} for m in watchlist],
        "dismissed_recommendations": [{"user_id": USER, "movie_id": m, "scope": "movie"} for m in dismissed],
    }, missing_columns)
    sig = signals(affinity={16: 8.0, 35: 4.0} if affinity is None else affinity,
                  reviewed=reviewed, watchlist=watchlist, dismissed=dismissed, **signal_kw)
    discover = FakeDiscover()
    signal_loads: list[str] = []

    def load_signals(user_id, sb):
        signal_loads.append(user_id)
        return sig

    monkeypatch.setattr(daily_picks, "get_supabase", lambda: db)
    monkeypatch.setattr(daily_picks, "_load_user_signals", load_signals)
    monkeypatch.setattr(daily_picks.tmdb, "discover_movies", discover)
    monkeypatch.setattr(
        daily_picks.tmdb, "get_top_rated_movies",
        lambda page=1: {"results": [tmdb_film(900_000 + page * 100 + i) for i in range(20)]},
    )
    return SimpleNamespace(db=db, discover=discover, pool_ids=set(pool_ids), signal_loads=signal_loads)


def find_day(kind: str, user: str = USER, start: date = START) -> date:
    day = start
    while daily_picks.day_kind(user, day) != kind:
        day += timedelta(days=1)
    return day


def noon(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc)


def ids(payload: dict) -> list[int]:
    return [m["id"] for m in payload["results"]]


# ─── The days ────────────────────────────────────────────────────────────────

def test_branch_days_come_two_then_three_days_apart():
    for user in ("a", "b", USER, "0d9c3f6e-5b1a-4c7e-9f00-1234567890ab"):
        days = [START + timedelta(days=i) for i in range(60)]
        branch = [d for d in days if daily_picks.day_kind(user, d) == "branch"]
        gaps = [(b - a).days for a, b in zip(branch, branch[1:])]
        assert len(branch) == 24  # two in every five days
        assert set(gaps) == {2, 3}
        assert all(x != y for x, y in zip(gaps, gaps[1:]))  # alternating


def test_not_everyone_branches_on_the_same_day():
    kinds = Counter(daily_picks.day_kind(f"user-{i}", START) for i in range(100))
    assert kinds["branch"] > 10 and kinds["shuffle"] > 10


def test_the_rhythm_is_stable_across_processes():
    # sha256, not hash(): hash() is salted per process, which would make the
    # rhythm (and every seeded choice) change on each gunicorn restart.
    rhythm = [daily_picks.day_kind(USER, START + timedelta(days=i)) for i in range(5)]
    assert rhythm == ["shuffle", "branch", "shuffle", "branch", "shuffle"]


def test_the_day_turns_over_at_the_users_midnight():
    now = datetime(2026, 10, 6, 14, 30, tzinfo=timezone.utc)  # 01:30 on the 7th in Sydney (AEDT)
    assert daily_picks.local_day(now, ZoneInfo("Australia/Sydney")) == date(2026, 10, 7)
    assert daily_picks.local_day(now, UTC) == date(2026, 10, 6)


# ─── Shuffle days ────────────────────────────────────────────────────────────

def test_pool_picks_lean_toward_the_top_of_for_you_but_reach_the_tail():
    pool = pool_items(range(40))
    counts: Counter = Counter()
    for seed in range(400):
        for pick in daily_picks.pick_from_pool(pool, set(), 3, random.Random(seed)):
            counts[pick["movie_id"]] += 1
    top, tail = sum(counts[m] for m in range(10)), sum(counts[m] for m in range(30, 40))
    assert top > 5 * tail
    assert tail > 0


def test_excluding_a_pick_moves_the_rest_up_without_reshuffling():
    pool = pool_items(range(20))
    full = [p["movie_id"] for p in daily_picks.pick_from_pool(pool, set(), 5, random.Random("seed"))]
    rest = [p["movie_id"] for p in daily_picks.pick_from_pool(pool, {full[0]}, 4, random.Random("seed"))]
    assert rest == full[1:]


def test_pool_picks_keep_their_for_you_reason():
    picks = daily_picks.pick_from_pool(
        [{"movie_id": 1, "reason": "Because you liked Alien"}, {"movie_id": 2, "reason": None}], set(), 3, random.Random(1)
    )
    assert sorted(picks, key=lambda p: p["movie_id"]) == [
        {"movie_id": 1, "reason": "Because you liked Alien", "source": "shuffle"},
        {"movie_id": 2, "reason": "Picked for you", "source": "shuffle"},
    ]


# ─── Branch days ─────────────────────────────────────────────────────────────

def all_pairs(sig, seeds=range(30)) -> set[tuple[int, int]]:
    return {pair for seed in seeds for pair in daily_picks.branch_pairs(sig, random.Random(seed))}


def test_a_branch_pairs_a_favourite_genre_with_one_outside_the_favourites():
    sig = signals(affinity={28: 9.0, 35: 5.0, 18: 3.0, 27: 2.0, 53: 1.0})
    favourites = {28, 35, 18, 27}  # the top four are the anchors
    for seed in range(30):
        pairs = daily_picks.branch_pairs(sig, random.Random(seed))
        assert 0 < len(pairs) <= daily_picks.MAX_PAIRS
        assert len(set(pairs)) == len(pairs)
        for anchor, other in pairs:
            assert anchor in favourites
            assert other not in favourites and other != 10770
    assert any(other == 53 for _, other in all_pairs(sig))  # a mild favourite can be the new half


def test_a_branch_never_reaches_into_a_genre_the_user_rates_down():
    sig = signals(affinity={28: 9.0, 35: 6.0, 18: 4.0, 80: 3.0, 10749: 0.5}, penalty={27: 4.0, 10749: 2.0})
    others = {other for _, other in all_pairs(sig)}
    assert 27 not in others and 10749 not in others
    assert 53 in others


def test_a_branch_skips_a_pairing_dismissed_as_not_my_type():
    sig = signals(affinity={28: 9.0}, type_dislikes=[{28, 27, 53}])
    pairs = all_pairs(sig)
    assert (28, 27) not in pairs and (28, 53) not in pairs
    assert (28, 18) in pairs


def test_no_branching_without_a_genre_the_user_likes():
    assert daily_picks.branch_pairs(signals(), random.Random(1)) == []
    assert daily_picks.branch_pairs(signals(affinity={28: -2.0}), random.Random(1)) == []


def test_branch_picks_share_one_pairing_and_name_only_the_familiar_genre():
    db = FakeSupabase({"movies": []})
    discover = FakeDiscover()
    sig = signals(affinity={16: 8.0, 35: 4.0})
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(daily_picks.tmdb, "discover_movies", discover)
        picks = daily_picks.pick_branch(sig, set(), 3, random.Random(5), db)

    assert len(picks) == 3 and {p["source"] for p in picks} == {"branch"}
    assert len({p["reason"] for p in picks}) == 1
    assert picks[0]["reason"] in {"Because you like Animation", "Because you like Comedy"}

    query = discover.calls[0]
    anchor, other = (int(g) for g in query["with_genres"].split(","))
    assert anchor in {16, 35} and other not in {16, 35}
    assert query["without_genres"] == "10770"
    assert query["vote_average.gte"] == daily_picks.BRANCH_MIN_RATING
    assert query["vote_count.gte"] == daily_picks.BRANCH_MIN_VOTES[query["sort_by"]]

    # Every pick is cached for _hydrate, insert-only, and none lacks a poster.
    assert {m["id"] for m in db.tables["movies"]} == {p["movie_id"] for p in picks}
    assert all(m["poster_path"] for m in db.tables["movies"])
    assert all(w[3].get("ignore_duplicates") is True for w in db.writes("upsert", "movies"))


def test_branch_picks_skip_excluded_films_and_keep_the_rest(monkeypatch):
    monkeypatch.setattr(daily_picks.tmdb, "discover_movies", FakeDiscover())
    sig = signals(affinity={16: 8.0})
    first = daily_picks.pick_branch(sig, set(), 3, random.Random(9), FakeSupabase())
    again = daily_picks.pick_branch(sig, {first[0]["movie_id"]}, 3, random.Random(9), FakeSupabase())
    assert [p["movie_id"] for p in again][:2] == [p["movie_id"] for p in first][1:]
    assert again[2]["movie_id"] not in {p["movie_id"] for p in first}


def test_films_without_a_poster_or_title_are_never_picked(monkeypatch):
    unusable = [tmdb_film(m, poster=False) for m in range(1, 10)] + [{**tmdb_film(50), "title": None}]
    monkeypatch.setattr(daily_picks.tmdb, "discover_movies", lambda params: {"results": unusable})
    assert daily_picks.pick_branch(signals(affinity={16: 8.0}), set(), 3, random.Random(1), FakeSupabase()) == []


def test_a_failing_pairing_falls_through_to_the_next(monkeypatch):
    discover = FakeDiscover()

    def flaky(params):
        if not discover.calls:
            discover.calls.append(params)
            raise RuntimeError("TMDB timed out")
        return discover(params)

    monkeypatch.setattr(daily_picks.tmdb, "discover_movies", flaky)
    picks = daily_picks.pick_branch(signals(affinity={16: 8.0}), set(), 3, random.Random(2), FakeSupabase())
    assert len(picks) == 3
    assert len(discover.calls) >= 2


# ─── Planning a day ──────────────────────────────────────────────────────────

def test_a_shuffle_day_draws_from_for_you(monkeypatch):
    env = setup(monkeypatch)
    day = find_day("shuffle")
    picks = daily_picks.plan(USER, day, env.db, 3, set())
    assert len(picks) == 3
    assert {p["source"] for p in picks} == {"shuffle"}
    assert {p["movie_id"] for p in picks} <= env.pool_ids
    assert {p["day"] for p in picks} == {day.isoformat()}
    assert not env.discover.calls
    assert not env.signal_loads  # the expensive taste profile is only for branching


def test_a_branch_day_reaches_outside_for_you(monkeypatch):
    env = setup(monkeypatch)
    picks = daily_picks.plan(USER, find_day("branch"), env.db, 3, set())
    assert len(picks) == 3
    assert {p["source"] for p in picks} == {"branch"}
    assert not {p["movie_id"] for p in picks} & env.pool_ids


def test_a_branch_day_never_shows_a_film_already_in_for_you(monkeypatch):
    env = setup(monkeypatch)
    discover = env.discover

    def overlapping(params):
        found = discover(params)
        return {"results": [tmdb_film(m) for m in sorted(env.pool_ids)] + found["results"]}

    monkeypatch.setattr(daily_picks.tmdb, "discover_movies", overlapping)
    for i in range(10):
        day = find_day("branch", start=START + timedelta(days=i * 5))
        assert not {p["movie_id"] for p in daily_picks.plan(USER, day, env.db, 3, set())} & env.pool_ids


def test_rated_saved_dismissed_and_recent_films_are_never_offered(monkeypatch):
    env = setup(monkeypatch, pool_ids=range(100, 112), reviewed={100}, watchlist={101}, dismissed={102})
    for i in range(20):
        day = START + timedelta(days=i)
        picks = daily_picks.plan(USER, day, env.db, 3, shown={103, 104})
        assert len(picks) == 3
        assert not {p["movie_id"] for p in picks} & {100, 101, 102, 103, 104}


def test_a_shuffle_day_without_for_you_yet_branches_out_instead(monkeypatch):
    env = setup(monkeypatch, pool_ids=[])
    picks = daily_picks.plan(USER, find_day("shuffle"), env.db, 3, set())
    assert [p["source"] for p in picks] == ["branch"] * 3


def test_a_branch_day_with_nothing_to_branch_from_shuffles(monkeypatch):
    env = setup(monkeypatch, affinity={})
    picks = daily_picks.plan(USER, find_day("branch"), env.db, 3, set())
    assert [p["source"] for p in picks] == ["shuffle"] * 3


def test_a_brand_new_user_still_gets_three(monkeypatch):
    env = setup(monkeypatch, pool_ids=[], affinity={})
    picks = daily_picks.plan(USER, START, env.db, 3, set())
    assert [p["source"] for p in picks] == ["backfill"] * 3
    assert {p["movie_id"] for p in picks} <= {m["id"] for m in env.db.tables["movies"]}


# ─── A day's picks, end to end ───────────────────────────────────────────────

def test_the_picks_hold_all_day_and_change_the_next(monkeypatch):
    env = setup(monkeypatch)
    day = find_day("shuffle")
    first = ids(daily_picks.get_daily_picks(USER, UTC, now=noon(day)))
    later = ids(daily_picks.get_daily_picks(USER, UTC, now=noon(day) + timedelta(hours=11)))
    assert len(first) == 3 and later == first
    assert len(env.db.writes("upsert", "user_weekly_picks")) == 1  # the second fetch only read

    tomorrow = ids(daily_picks.get_daily_picks(USER, UTC, now=noon(day + timedelta(days=1))))
    assert len(tomorrow) == 3 and not set(tomorrow) & set(first)


def test_nothing_comes_back_within_two_weeks(monkeypatch):
    setup(monkeypatch, pool_ids=range(100, 160))
    seen: set[int] = set()
    for i in range(daily_picks.RECENT_DAYS):
        picks = ids(daily_picks.get_daily_picks(USER, UTC, now=noon(START + timedelta(days=i))))
        assert len(picks) == 3
        assert not set(picks) & seen
        seen |= set(picks)


def test_the_history_forgets_after_two_weeks(monkeypatch):
    env = setup(monkeypatch)
    for i in range(daily_picks.RECENT_DAYS + 6):
        daily_picks.get_daily_picks(USER, UTC, now=noon(START + timedelta(days=i)))
    recent = env.db.tables["user_weekly_picks"][0]["recent"]
    cutoff = START + timedelta(days=6)  # the last fourteen days, today included
    assert recent and all(date.fromisoformat(e["day"]) >= cutoff for e in recent)
    assert len(recent) == 3 * daily_picks.RECENT_DAYS


def test_the_day_follows_the_callers_timezone(monkeypatch):
    env = setup(monkeypatch)
    daily_picks.get_daily_picks(USER, ZoneInfo("Australia/Sydney"), now=datetime(2026, 10, 6, 13, 30, tzinfo=timezone.utc))
    assert {it["day"] for it in env.db.tables["user_weekly_picks"][0]["items"]} == {"2026-10-07"}


def test_a_branch_day_looks_like_any_other_day(monkeypatch):
    setup(monkeypatch)
    payload = daily_picks.get_daily_picks(USER, UTC, now=noon(find_day("branch")))
    assert len(payload["results"]) == 3
    for film in payload["results"]:
        assert film["reason"].startswith("Because you like ")
        assert not {"source", "day"} & set(film)


def test_last_weeks_picks_are_replaced_and_not_shown_again(monkeypatch):
    weekly_row = {
        "user_id": USER,
        "items": [{"movie_id": m, "reason": "Old pick"} for m in (100, 101, 102)],
        "computed_at": "2026-10-01T00:00:00+00:00",
    }
    setup(monkeypatch, row=weekly_row)
    picks = ids(daily_picks.get_daily_picks(USER, UTC, now=noon(find_day("shuffle"))))
    assert len(picks) == 3 and not set(picks) & {100, 101, 102}


def test_a_dismissed_pick_is_replaced_and_the_other_two_stay(monkeypatch):
    for kind in ("shuffle", "branch"):
        env = setup(monkeypatch)
        now = noon(find_day(kind))
        first = daily_picks.get_daily_picks(USER, UTC, now=now)["results"]

        dropped = first[1]["id"]
        assert daily_picks.drop_from_daily_picks(env.db, USER, dropped) is True
        # What mark_not_interested records.
        env.db.tables["dismissed_recommendations"].append({"user_id": USER, "movie_id": dropped, "scope": "movie"})

        after = daily_picks.get_daily_picks(USER, UTC, now=now + timedelta(minutes=1))["results"]
        assert [m["id"] for m in after][:2] == [first[0]["id"], first[2]["id"]]
        assert len(after) == 3 and after[2]["id"] not in {m["id"] for m in first}
        if kind == "branch":
            assert after[2]["reason"] == first[0]["reason"]  # same theme as the rest of the day


def test_dropping_a_film_that_is_not_a_pick_writes_nothing(monkeypatch):
    env = setup(monkeypatch)
    daily_picks.get_daily_picks(USER, UTC, now=noon(START))
    assert daily_picks.drop_from_daily_picks(env.db, USER, 424242) is False
    assert not env.db.writes("update", "user_weekly_picks")
    assert daily_picks.drop_from_daily_picks(FakeSupabase(), USER, 1) is False  # no row at all


def test_works_before_sql_010_is_applied(monkeypatch):
    env = setup(monkeypatch, missing_columns={"user_weekly_picks": {"recent"}})
    day_one = ids(daily_picks.get_daily_picks(USER, UTC, now=noon(START)))
    day_two = ids(daily_picks.get_daily_picks(USER, UTC, now=noon(START + timedelta(days=1))))
    assert len(day_one) == len(day_two) == 3
    assert not set(day_one) & set(day_two)  # yesterday's are still held back
    assert "recent" not in env.db.tables["user_weekly_picks"][0]


# ─── Routes ──────────────────────────────────────────────────────────────────

AUTH = {"Authorization": "Bearer good"}


@pytest.fixture
def client(monkeypatch):
    app = create_app()
    app.config.update(TESTING=True, RATELIMIT_ENABLED=False)
    monkeypatch.setattr(auth_utils, "_validate_token", lambda token: SimpleNamespace(id=USER, factors=[]) if token == "good" else None)
    with app.test_client() as c:
        yield c


def test_the_route_uses_the_callers_timezone(client, monkeypatch):
    seen = {}

    def fake(user_id, tz, force=False, now=None):
        seen.update(user_id=user_id, tz=str(tz), force=force)
        return {"results": []}

    monkeypatch.setattr(movies_controller.daily_picks, "get_daily_picks", fake)
    assert client.get("/api/movies/movies-of-the-day?tz=Australia/Sydney", headers=AUTH).status_code == 200
    assert seen == {"user_id": USER, "tz": "Australia/Sydney", "force": False}
    assert client.get("/api/movies/movies-of-the-day?tz=Mars/Olympus_Mons", headers=AUTH).status_code == 400
    assert client.get("/api/movies/movies-of-the-day").status_code == 401

    # The pre-rename path still answers, on UTC days.
    assert client.get("/api/movies/picks-of-the-week", headers=AUTH).status_code == 200
    assert seen["tz"] == "UTC"


def test_not_interested_takes_the_film_out_of_todays_picks(client, monkeypatch):
    db = FakeSupabase({"user_weekly_picks": [{
        "user_id": USER, "recent": [],
        "items": [{"movie_id": m, "reason": "r", "day": "2026-10-06", "source": "shuffle"} for m in (1, 2, 3)],
    }]})
    monkeypatch.setattr(movies_controller, "get_supabase", lambda: db)
    monkeypatch.setattr(movies_controller.recommendations, "mark_not_interested", lambda user_id, movie_id, scope: None)

    res = client.post("/api/movies/not-interested", json={"movie_id": 2}, headers=AUTH)
    assert res.status_code == 200
    assert res.get_json() == {"replacement": None, "daily_pick_dropped": True}
    assert [it["movie_id"] for it in db.tables["user_weekly_picks"][0]["items"]] == [1, 3]

    res = client.post("/api/movies/not-interested", json={"movie_id": 99}, headers=AUTH)
    assert res.get_json()["daily_pick_dropped"] is False


def test_a_failed_daily_drop_does_not_fail_the_dismissal(client, monkeypatch):
    def boom(*_args):
        raise RuntimeError("PostgREST unavailable")

    monkeypatch.setattr(movies_controller.recommendations, "mark_not_interested", lambda user_id, movie_id, scope: {"id": 5})
    monkeypatch.setattr(movies_controller.daily_picks, "drop_from_daily_picks", boom)
    monkeypatch.setattr(movies_controller, "get_supabase", lambda: FakeSupabase())
    res = client.post("/api/movies/not-interested", json={"movie_id": 2}, headers=AUTH)
    assert res.status_code == 200
    assert res.get_json() == {"replacement": {"id": 5}, "daily_pick_dropped": False}
