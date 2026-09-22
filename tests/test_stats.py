"""Pure-maths tests for the taste dashboard and Wrapped. Everything is built
from plain dicts with an injected clock and timezone — no network, no Flask
app context.

The dashboard is deliberately thin (totals + genres), so the section builders
it no longer calls are tested directly; they are what the Wrapped is made of.
"""
import itertools
import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.services import stats
from app.services.pg import chunked, paginate
from app.services.stats import (
    TasteData,
    compute_dashboard,
    compute_wrapped,
    film_rows,
    pick_persona,
    unlock_status,
    wrapped_years,
)

UTC = timezone.utc
SYD = ZoneInfo("Australia/Sydney")
NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)  # a Monday
DAY = timedelta(days=1)

_ids = itertools.count(1)


def person(pid: int, name: str) -> dict:
    return {"id": pid, "name": name, "profile_path": None}


def make_movie(mid: int, **over) -> dict:
    movie = {
        "id": mid,
        "title": f"Film {mid}",
        "poster_path": f"/p{mid}.jpg",
        "backdrop_path": None,
        "release_date": "2010-05-01",
        "runtime": 110,
        "genre_ids": [18],
        "vote_average": 7.0,
        "vote_count": 1000,
        "popularity": 40.0,
        "original_language": "en",
        "production_countries": ["US"],
        "budget": None,
        "revenue": None,
        "collection_id": None,
        "collection_name": None,
        "directors": None,
        "top_cast": None,
        "tagline": None,
    }
    if over.pop("no_extras", False):
        movie.update(original_language=None, production_countries=[], popularity=None, vote_count=None)
    movie.update(over)
    return movie


def make_review(movie: dict, rating: float, created_at: datetime, **over) -> dict:
    row = {
        "id": f"r{next(_ids)}",
        "movie_id": movie["id"],
        "rating": rating,
        "review_text": "",
        "rewatch_count": 0,
        "category_ids": [],
        "is_onboarding": False,
        "created_at": created_at.isoformat(),
        "movies": movie,
    }
    row.update(over)
    return row


def make_data(reviews=(), watchlist=(), friends=(), friend_reviews=()) -> TasteData:
    return TasteData(
        user_id="me",
        reviews=list(reviews),
        watchlist=list(watchlist),
        friends=list(friends),
        friend_reviews=list(friend_reviews),
    )


def dashboard(data: TasteData, now: datetime = NOW, tz=UTC) -> dict:
    return compute_dashboard(data, now=now, tz=tz)


def films_of(data: TasteData) -> list:
    """The normalised rows every section builder takes."""
    return film_rows(data.reviews)


def year_of_films(count: int, year: int = 2026, **movie_over) -> list[dict]:
    return [
        make_review(make_movie(i, **movie_over), 5 if i % 2 else 2, datetime(year, 1 + (i % 9), 10, 20, 0, tzinfo=UTC))
        for i in range(1, count + 1)
    ]


# ─── Normalisation ───────────────────────────────────────────────────────────

def test_dedupe_keeps_newest_review_per_movie():
    movie = make_movie(1)
    rows = [
        make_review(movie, 2, NOW - 10 * DAY),
        make_review(movie, 5, NOW - 1 * DAY),
    ]
    films = film_rows(rows)
    assert len(films) == 1
    assert films[0].rating == 5


def test_rows_without_movie_or_timestamp_are_skipped():
    movie = make_movie(1)
    no_timestamp = make_review(movie, 4, NOW)
    no_timestamp["created_at"] = None
    rows = [
        make_review(movie, 4, NOW, movies=None),
        no_timestamp,
        make_review(make_movie(2), 4, NOW),
    ]
    assert [f.movie_id for f in film_rows(rows)] == [2]


# ─── Dashboard: what it says, and what it refuses to ─────────────────────────

DASHBOARD_KEYS = {"generated_at", "tz", "coverage", "headline", "genres", "genre_highlights", "watchlist"}


def test_dashboard_withholds_everything_the_wrapped_reveals():
    """The reveals are kept out of the payload, not just out of the UI — the
    network tab must not spoil the Wrapped either."""
    reviews = year_of_films(12, directors=[person(1, "Greta Gerwig")], top_cast=[person(2, "Saoirse Ronan")])
    watchlist = [{"movie_id": 90, "added_at": (NOW - 40 * DAY).isoformat(), "movies": make_movie(90, title="Waiting Film")}]
    friends = [{"id": "a", "username": "alice", "avatar_url": None, "avatar_color": None, "avatar_focal_y": None, "avatar_zoom": None}]
    friend_reviews = [{"user_id": "a", "movie_id": i, "rating": 4, "created_at": NOW.isoformat()} for i in (1, 2, 3)]
    out = dashboard(make_data(reviews, watchlist=watchlist, friends=friends, friend_reviews=friend_reviews))

    assert set(out) == DASHBOARD_KEYS
    assert set(out["watchlist"]) == {"count", "total_minutes"}

    blob = json.dumps(out)
    for spoiler in ("Film 1", "Waiting Film", "Greta Gerwig", "Saoirse Ronan", "alice", "poster_path"):
        assert spoiler not in blob


def test_empty_data_produces_full_shape_with_zeros():
    out = dashboard(make_data())
    assert set(out) == DASHBOARD_KEYS
    assert out["headline"]["films"] == 0
    assert out["headline"]["avg_rating"] is None
    assert out["headline"]["first_rated_at"] is None
    assert out["genres"] == []
    assert out["genre_highlights"]["most_watched"] is None
    assert out["coverage"] == {"films": 0, "with_runtime": 0, "with_people": 0, "with_extras": 0}
    assert out["watchlist"] == {"count": 0, "total_minutes": 0}


def test_headline_totals_and_rewatch_weighted_minutes():
    m1 = make_movie(1, runtime=100)
    m2 = make_movie(2, runtime=90)
    data = make_data([
        make_review(m1, 5, NOW - DAY, rewatch_count=2, review_text="great film really"),
        make_review(m2, 3, NOW - 2 * DAY),
    ])
    head = dashboard(data)["headline"]
    assert head["films"] == 2
    assert head["watch_minutes"] == 100 * 3 + 90
    assert head["avg_rating"] == 4.0
    assert head["rewatches"] == 2
    assert head["rewatched_films"] == 1
    assert head["written_reviews"] == 1
    assert head["written_words"] == 3


def test_watchlist_tile_counts_films_and_runtime():
    watchlist = [
        {"movie_id": 10, "added_at": (NOW - 30 * DAY).isoformat(), "movies": make_movie(10, runtime=90)},
        {"movie_id": 11, "added_at": (NOW - 3 * DAY).isoformat(), "movies": make_movie(11, runtime=100)},
    ]
    assert dashboard(make_data([], watchlist=watchlist))["watchlist"] == {"count": 2, "total_minutes": 190}


def test_onboarding_counts_all_time_but_never_in_a_wrapped():
    movie = make_movie(1)
    data = make_data([make_review(movie, 4, NOW - DAY, is_onboarding=True)])
    out = dashboard(data)
    assert out["headline"]["films"] == 1
    assert out["headline"]["films_excluding_onboarding"] == 0
    wrapped = compute_wrapped(data, 2026, now=NOW, tz=UTC, min_films=1)
    assert wrapped["status"] == "not_enough"
    assert wrapped["films"] == 0


# ─── Genres (the one breakdown the profile keeps) ────────────────────────────

def test_genre_table_affinity_and_highlight_gates():
    horror = make_movie(1, genre_ids=[27])
    horror2 = make_movie(2, genre_ids=[27])
    horror3 = make_movie(3, genre_ids=[27, 18])
    drama = make_movie(4, genre_ids=[18])
    data = make_data([
        make_review(horror, 5, NOW - DAY),
        make_review(horror2, 4, NOW - 2 * DAY),
        make_review(horror3, 5, NOW - 3 * DAY),
        make_review(drama, 1, NOW - 4 * DAY),
    ])
    out = dashboard(data)
    by_name = {g["name"]: g for g in out["genres"]}
    assert by_name["Horror"]["count"] == 3
    assert by_name["Horror"]["affinity"] == (5 - 3) + (4 - 3) + (5 - 3)
    assert by_name["Drama"]["count"] == 2
    assert by_name["Drama"]["affinity"] == (5 - 3) + (1 - 3)
    highlights = out["genre_highlights"]
    assert highlights["most_watched"]["name"] == "Horror"
    # highest_rated needs >= 3 films: Drama (2 films) can't win it
    assert highlights["highest_rated"]["name"] == "Horror"
    assert highlights["lowest_rated"] is None
    assert [g["name"] for g in highlights["affinity_top"]] == ["Horror"]


# ─── Section builders (the Wrapped's raw material) ───────────────────────────

def test_hot_take_guards_and_deltas():
    beloved = make_movie(1, vote_average=8.6, vote_count=5000)      # you: 1 star -> delta -6.6
    thin = make_movie(2, vote_average=9.5, vote_count=10)           # too few votes: ignored
    unrated = make_movie(3, vote_average=0, vote_count=None)        # no crowd score: ignored
    gem = make_movie(4, vote_average=5.0, vote_count=200)           # you: 5 stars -> delta +5
    data = make_data([
        make_review(beloved, 1, NOW - DAY),
        make_review(thin, 5, NOW - 2 * DAY),
        make_review(unrated, 5, NOW - 3 * DAY),
        make_review(gem, 5, NOW - 4 * DAY),
    ])
    vs = stats._vs_world(films_of(data))
    assert vs["sample_size"] == 2
    assert vs["mean_delta"] == pytest.approx((-6.6 + 5.0) / 2, abs=0.01)
    assert vs["label"] == "harsher"
    assert [t["movie"]["id"] for t in vs["hot_takes"]["loved_more"]] == [4]
    assert [t["movie"]["id"] for t in vs["hot_takes"]["loved_less"]] == [1]
    assert vs["hot_takes"]["loved_less"][0]["delta"] == pytest.approx(-6.6)


def test_agreement_share_counts_films_within_one_star():
    close = make_movie(1, vote_average=9.0, vote_count=800)   # you 5 -> delta +1.0, inside a star
    far = make_movie(2, vote_average=4.0, vote_count=800)     # you 5 -> delta +6.0
    data = make_data([make_review(close, 5, NOW - DAY), make_review(far, 5, NOW - 2 * DAY)])
    assert stats._vs_world(films_of(data))["agreement_share"] == 0.5


def test_people_most_watched_and_highest_rated_gate():
    nolan, villeneuve = person(1, "Christopher Nolan"), person(2, "Denis Villeneuve")
    data = make_data([
        make_review(make_movie(1, directors=[nolan], top_cast=[person(10, "Cillian")]), 3, NOW - DAY),
        make_review(make_movie(2, directors=[nolan]), 4, NOW - 2 * DAY),
        make_review(make_movie(3, directors=[villeneuve]), 5, NOW - 3 * DAY),
    ])
    people = stats._people(films_of(data))
    assert people["directors"]["most_watched"][0]["name"] == "Christopher Nolan"
    assert people["directors"]["most_watched"][0]["count"] == 2
    assert people["directors"]["most_watched"][0]["avg_rating"] == 3.5
    # highest_rated needs >= 2 films — Villeneuve's single 5 doesn't qualify
    assert [p["name"] for p in people["directors"]["highest_rated"]] == ["Christopher Nolan"]
    assert people["actors"]["most_watched"][0]["name"] == "Cillian"


def test_friend_compatibility_twin_nemesis_and_gate():
    movies = {i: make_movie(i) for i in range(1, 5)}
    mine = [make_review(movies[1], 5, NOW - DAY), make_review(movies[2], 4, NOW - 2 * DAY),
            make_review(movies[3], 3, NOW - 3 * DAY), make_review(movies[4], 2, NOW - 4 * DAY)]
    friends = [
        {"id": "a", "username": "alice", "avatar_url": None, "avatar_color": None, "avatar_focal_y": None, "avatar_zoom": None},
        {"id": "b", "username": "bob", "avatar_url": None, "avatar_color": None, "avatar_focal_y": None, "avatar_zoom": None},
        {"id": "c", "username": "cam", "avatar_url": None, "avatar_color": None, "avatar_focal_y": None, "avatar_zoom": None},
    ]
    fr = [
        {"user_id": "a", "movie_id": 1, "rating": 5, "created_at": NOW.isoformat()},
        {"user_id": "a", "movie_id": 2, "rating": 4, "created_at": NOW.isoformat()},
        {"user_id": "a", "movie_id": 3, "rating": 2, "created_at": NOW.isoformat()},
        {"user_id": "b", "movie_id": 1, "rating": 1, "created_at": NOW.isoformat()},
        {"user_id": "b", "movie_id": 2, "rating": 2, "created_at": NOW.isoformat()},
        {"user_id": "b", "movie_id": 3, "rating": 3, "created_at": NOW.isoformat()},
        {"user_id": "c", "movie_id": 1, "rating": 5, "created_at": NOW.isoformat()},
        {"user_id": "c", "movie_id": 2, "rating": 4, "created_at": NOW.isoformat()},
    ]
    data = make_data(mine, friends=friends, friend_reviews=fr)
    out = stats._friends(data, films_of(data))
    assert out["friend_count"] == 3
    assert [c["username"] for c in out["compared"]] == ["alice", "bob"]  # cam: only 2 shared
    assert out["twin"]["username"] == "alice"
    assert out["twin"]["compatibility"] == pytest.approx(1 - (1 / 3) / 4, abs=0.001)
    assert out["nemesis"]["username"] == "bob"
    assert out["nemesis"]["most_disagreed"]["movie"]["id"] == 1
    assert out["nemesis"]["most_disagreed"]["your_rating"] == 5
    assert out["nemesis"]["most_disagreed"]["their_rating"] == 1


def test_streaks_with_grace_week_and_gaps():
    today = date(2026, 9, 21)  # a Monday
    days = [date(2026, 8, 17), date(2026, 9, 7), date(2026, 9, 14)]
    # Two consecutive weeks ending *last* week still counts as current.
    assert stats._streaks(days, today) == (2, 2)


def test_current_streak_breaks_after_two_quiet_weeks():
    assert stats._streaks([date(2026, 8, 31)], date(2026, 9, 21)) == (0, 1)


def test_watchlist_oldest_and_genre_gap():
    reviews = [make_review(make_movie(1, genre_ids=[18]), 4, NOW - DAY)]
    watchlist = [
        {"movie_id": 10, "added_at": (NOW - 30 * DAY).isoformat(), "movies": make_movie(10, genre_ids=[27], runtime=90)},
        {"movie_id": 11, "added_at": (NOW - 3 * DAY).isoformat(), "movies": make_movie(11, genre_ids=[27], runtime=100)},
        {"movie_id": 12, "added_at": (NOW - 2 * DAY).isoformat(), "movies": make_movie(12, genre_ids=[18], runtime=None)},
    ]
    data = make_data(reviews, watchlist=watchlist)
    wl = stats._watchlist(data, films_of(data), NOW)
    assert wl["count"] == 3
    assert wl["total_minutes"] == 190
    assert wl["oldest"]["movie"]["id"] == 10
    assert wl["oldest"]["days_waiting"] == 30
    assert wl["genre_gap"][0]["name"] == "Horror"


def test_runtime_profile():
    data = make_data([
        make_review(make_movie(1, runtime=80), 3, NOW - DAY),
        make_review(make_movie(2, runtime=150), 5, NOW - 2 * DAY),
        make_review(make_movie(3, runtime=None), 4, NOW - 3 * DAY),
    ])
    rt = stats._runtime(films_of(data))
    assert rt["sample_size"] == 2
    assert rt["avg_minutes"] == 115
    assert rt["longest"]["id"] == 2
    assert rt["share_under_90m"] == 0.5
    assert rt["share_over_2h"] == 0.5


def test_extras_requires_coverage_then_reports_world_money_and_franchises():
    thin = make_data([make_review(make_movie(i), 4, NOW - i * DAY) for i in range(1, 6)])
    assert stats._extras(films_of(thin)) is None

    reviews = []
    for i in range(1, 13):
        movie = make_movie(
            i,
            original_language="ko" if i % 3 == 0 else "en",
            production_countries=["KR"] if i % 3 == 0 else ["US", "GB"],
            budget=150_000_000 if i % 2 == 0 else 1_000_000,
            popularity=5.0 if i == 1 else 40.0,
            collection_id=7 if i in (2, 4, 6) else None,
            collection_name="Big Saga" if i in (2, 4, 6) else None,
        )
        reviews.append(make_review(movie, 5 if i == 1 else 3, NOW - i * DAY))
    extras = stats._extras(films_of(make_data(reviews)))
    assert extras["languages"]["count"] == 2
    assert extras["languages"]["non_english_share"] == pytest.approx(4 / 12, abs=0.001)
    assert extras["countries"]["count"] == 3
    assert extras["budget"]["blockbuster_count"] == 6
    assert extras["budget"]["indie_count"] == 6
    assert extras["hidden_gems"][0]["movie"]["id"] == 1
    assert extras["franchises"][0]["collection_id"] == 7
    assert extras["franchises"][0]["count"] == 3


# ─── Wrapped ─────────────────────────────────────────────────────────────────

def test_wrapped_not_enough_below_threshold():
    out = compute_wrapped(make_data(year_of_films(4)), 2026, now=NOW, tz=UTC, min_films=5)
    assert out == {"status": "not_enough", "year": 2026, "films": 4, "min_films": 5,
                   "generated_at": NOW.isoformat(), "tz": "UTC"}


def test_wrapped_ready_has_ordered_core_slides_and_summary():
    reviews = year_of_films(8, directors=[person(1, "Greta Gerwig")], top_cast=[person(2, "Saoirse")])
    reviews.append(make_review(make_movie(99), 4, datetime(2025, 6, 1, tzinfo=UTC)))  # previous year: ignored
    out = compute_wrapped(make_data(reviews), 2026, now=NOW, tz=UTC, min_films=5)
    assert out["status"] == "ready"
    assert out["films"] == 8
    kinds = [s["kind"] for s in out["slides"]]
    assert kinds[:3] == ["intro", "volume", "runtime"]
    assert kinds[-2:] == ["persona", "summary"]
    for kind in ("months", "genres", "eras", "people", "loves", "hates", "hot_take", "critic"):
        assert kind in kinds
    assert "rewatches" not in kinds and "words" not in kinds and "friends" not in kinds
    loves = next(s for s in out["slides"] if s["kind"] == "loves")
    assert loves["film_of_the_year"]["rating"] == 5
    hates = next(s for s in out["slides"] if s["kind"] == "hates")
    assert hates["disliked_count"] == 4
    people = next(s for s in out["slides"] if s["kind"] == "people")
    assert people["director"]["name"] == "Greta Gerwig" and people["director"]["count"] == 8
    assert out["summary"]["top_director"]["name"] == "Greta Gerwig"
    assert out["persona"]["key"] in stats.PERSONAS
    assert len(out["persona"]["evidence"]) >= 1
    months = next(s for s in out["slides"] if s["kind"] == "months")
    assert len(months["months"]) == 12
    assert sum(m["count"] for m in months["months"]) == 8


def test_wrapped_runtime_slide_carries_the_attention_span_stats():
    reviews = [make_review(make_movie(i, runtime=100 + i * 10), 4, datetime(2026, 3, i, tzinfo=UTC)) for i in range(1, 7)]
    out = compute_wrapped(make_data(reviews), 2026, now=NOW, tz=UTC, min_films=5)
    slide = next(s for s in out["slides"] if s["kind"] == "runtime")
    assert slide["longest"]["id"] == 6
    assert slide["avg_minutes"] == 135
    assert slide["theme"]


def test_wrapped_omits_the_runtime_slide_without_enough_runtimes():
    reviews = [make_review(make_movie(i, runtime=None), 4, datetime(2026, 3, i, tzinfo=UTC)) for i in range(1, 7)]
    out = compute_wrapped(make_data(reviews), 2026, now=NOW, tz=UTC, min_films=5)
    assert "runtime" not in [s["kind"] for s in out["slides"]]


def test_wrapped_hates_slide_always_present_even_with_no_duds():
    reviews = [make_review(make_movie(i), 5, datetime(2026, 2, i, tzinfo=UTC)) for i in range(1, 7)]
    out = compute_wrapped(make_data(reviews), 2026, now=NOW, tz=UTC, min_films=5)
    hates = next(s for s in out["slides"] if s["kind"] == "hates")
    assert hates["worst"] == [] and hates["disliked_count"] == 0


def test_wrapped_year_boundary_uses_local_timezone():
    nye = datetime(2025, 12, 31, 20, 0, tzinfo=UTC)  # already Jan 1 2026 in Sydney
    reviews = [make_review(make_movie(i), 4, nye + timedelta(minutes=i)) for i in range(1, 7)]
    data = make_data(reviews)
    assert wrapped_years([nye], SYD) == [2026]
    assert wrapped_years([nye], UTC) == [2025]
    assert compute_wrapped(data, 2026, now=NOW, tz=SYD, min_films=5)["status"] == "ready"
    assert compute_wrapped(data, 2026, now=NOW, tz=UTC, min_films=5)["status"] == "not_enough"
    assert compute_wrapped(data, 2025, now=NOW, tz=UTC, min_films=5)["status"] == "ready"


def test_wrapped_month_buckets_use_local_time():
    nye = datetime(2025, 12, 31, 20, 0, tzinfo=UTC)
    reviews = [make_review(make_movie(i), 4, nye + timedelta(minutes=i)) for i in range(1, 7)]
    syd = compute_wrapped(make_data(reviews), 2026, now=NOW, tz=SYD, min_films=5)
    months = next(s for s in syd["slides"] if s["kind"] == "months")
    assert months["busiest"] == {"month": 1, "count": 6}


# ─── Persona ─────────────────────────────────────────────────────────────────

def base_metrics(**over) -> dict:
    m = {
        "films": 20, "minutes": 2000, "genre_count": 5, "country_count": 2, "mean_release_year": 2015.0,
        "pre2000_share": 0.1, "recent_share": 0.2, "top_genre": "Drama", "top_genre_share": 0.3,
        "avg_rating": 3.5, "disliked_share": 0.1, "loved_share": 0.4, "agreement_share": 0.7,
        "agreement_sample": 15, "rewatches": 0, "max_director_count": 1, "top_director": "Someone",
        "words": 100, "night_share": 0.1, "franchises_3plus": 0,
    }
    m.update(over)
    return m


@pytest.mark.parametrize("over, key", [
    ({}, "cinephile"),
    ({"films": 120}, "marathoner"),
    ({"genre_count": 11}, "explorer"),
    ({"country_count": 7}, "explorer"),
    ({"mean_release_year": 1990.0}, "archaeologist"),
    ({"recent_share": 0.6}, "premiere_chaser"),
    ({"top_genre_share": 0.55}, "devotee"),
    ({"avg_rating": 2.5}, "harsh_critic"),
    ({"loved_share": 0.7}, "enthusiast"),
    ({"agreement_share": 0.3}, "contrarian"),
    ({"rewatches": 6}, "loyalist"),
    ({"max_director_count": 4}, "loyalist"),
    ({"words": 2500}, "wordsmith"),
    ({"night_share": 0.5}, "night_owl"),
    ({"franchises_3plus": 2}, "completionist"),
])
def test_persona_rules(over, key):
    persona = pick_persona(base_metrics(**over), 2026)
    assert persona["key"] == key
    assert persona["title"] == stats.PERSONAS[key]["title"]
    assert 1 <= len(persona["evidence"]) <= 3


def test_persona_priority_prefers_rarer_trait():
    # Marathoner + harsh critic -> marathoner (checked first)
    assert pick_persona(base_metrics(films=150, avg_rating=2.0), 2026)["key"] == "marathoner"
    # Contrarian needs a sample of >= 10
    assert pick_persona(base_metrics(agreement_share=0.2, agreement_sample=5), 2026)["key"] == "cinephile"


# ─── Unlock logic ────────────────────────────────────────────────────────────

def test_unlock_status_past_current_future():
    assert unlock_status(2025, now=NOW, unlock_month_day="12-01") == ("ready", None)
    assert unlock_status(2027, now=NOW, unlock_month_day="12-01") == ("future", None)
    status, at = unlock_status(2026, now=NOW, unlock_month_day="12-01")
    assert status == "locked"
    assert at == datetime(2026, 12, 1, tzinfo=UTC)


def test_unlock_status_on_and_after_the_date_and_custom_month_day():
    assert unlock_status(2026, now=datetime(2026, 12, 1, 0, 0, tzinfo=UTC), unlock_month_day="12-01") == ("ready", None)
    assert unlock_status(2026, now=datetime(2026, 12, 25, tzinfo=UTC), unlock_month_day="12-01") == ("ready", None)
    status, at = unlock_status(2026, now=datetime(2026, 12, 20, tzinfo=UTC), unlock_month_day="12-25")
    assert (status, at) == ("locked", datetime(2026, 12, 25, tzinfo=UTC))


def test_unlock_status_preview_and_invalid_config_fallback():
    assert unlock_status(2026, now=NOW, unlock_month_day="12-01", preview=True) == ("ready", None)
    status, at = unlock_status(2026, now=NOW, unlock_month_day="not-a-date")
    assert (status, at) == ("locked", datetime(2026, 12, 1, tzinfo=UTC))


def test_a_finished_year_stays_open_as_history():
    """New Year's Day: last year's Wrapped is no longer the current one, and
    must still be readable — that's the history."""
    new_year = datetime(2027, 1, 1, 0, 30, tzinfo=UTC)
    assert unlock_status(2026, now=new_year, unlock_month_day="12-01") == ("ready", None)
    assert unlock_status(2027, now=new_year, unlock_month_day="12-01")[0] == "locked"


# ─── PostgREST helpers ───────────────────────────────────────────────────────

class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeBuilder:
    def __init__(self, rows, calls):
        self._rows, self._calls = rows, calls

    def range(self, start, end):
        self._calls.append((start, end))
        self._slice = (start, end)
        return self

    def execute(self):
        start, end = self._slice
        return _FakeResult(self._rows[start:end + 1])


def test_paginate_walks_pages_until_a_short_one():
    rows = [{"i": i} for i in range(2500)]
    calls: list = []
    out = paginate(lambda: _FakeBuilder(rows, calls), page_size=1000)
    assert out == rows
    assert calls == [(0, 999), (1000, 1999), (2000, 2999)]


def test_paginate_exact_multiple_makes_one_extra_empty_call():
    rows = [{"i": i} for i in range(1000)]
    calls: list = []
    assert paginate(lambda: _FakeBuilder(rows, calls), page_size=1000) == rows
    assert calls == [(0, 999), (1000, 1999)]


def test_chunked():
    assert list(chunked(range(5), 2)) == [[0, 1], [2, 3], [4]]
    assert list(chunked([], 2)) == []
