"""One streaming service's own Discover sub-page (Popular/For You/Different),
scoped via TMDB's own with_watch_providers rather than a per-movie
availability bolt-on — see app.services.streaming_worlds' module docstring
for why that sidesteps the class of timeout bug the main streaming filter
needed several rounds to fix."""
from datetime import date
from types import SimpleNamespace

import pytest

from app import create_app
from app.services import streaming_worlds
from tests.fakes import FakeSupabase

# A fixed day for every test below — get_streaming_world's own randomized
# page/sort choice is seeded on (user_id, day, ...), so pinning the day
# keeps a test's outcome stable across runs even though, by design, it
# varies with the calendar date when the caller doesn't pass one.
DAY = date(2024, 1, 1)


@pytest.fixture
def app_ctx():
    with create_app().app_context():
        yield


@pytest.fixture(autouse=True)
def no_redis(monkeypatch):
    """get_streaming_world now wraps a Redis cache around
    _compute_streaming_world (see streaming_worlds.py) — a per-test dict
    store keeps every test's result independent and deterministic
    regardless of whether a real Redis happens to be reachable, since many
    tests below reuse the same (user_id, provider_id, day)."""
    store: dict = {}
    monkeypatch.setattr(streaming_worlds.cache, "cache_get", lambda key: store.get(key))
    monkeypatch.setattr(streaming_worlds.cache, "cache_set", lambda key, value, ttl=None: store.__setitem__(key, value))
    return store


def fake_signals(**overrides) -> SimpleNamespace:
    """A minimal stand-in for recommendations.UserSignals, carrying only the
    fields streaming_worlds.py itself reads (reviewed_ids, genre_affinity,
    onboarding_genre_ids) plus the three build_taste_profile_terms needs
    (positive_seeds, diversity_seeds, seed_info) so it doesn't AttributeError
    — empty by default, meaning "no taste profile", same as a real cold-start
    UserSignals would produce."""
    base = dict(
        reviewed_ids=set(), genre_affinity={}, onboarding_genre_ids=[],
        positive_seeds=[], diversity_seeds=[], seed_info={},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def movie(mid, genre_ids=(18,), vote_average=7.0):
    return {
        "id": mid, "title": f"Movie {mid}", "poster_path": f"/{mid}.jpg",
        "backdrop_path": None, "release_date": "2020-01-01",
        "vote_average": vote_average, "genre_ids": list(genre_ids),
    }


def install(monkeypatch, tables=None, by_params=None):
    """by_params: {frozenset(sorted(params.items())) subset check} -> movies.
    Simplified: a function (params) -> list[movie dict], so tests can shape
    results by sort_by/with_genres without hand-rolling TMDB's full query
    grammar."""
    db = FakeSupabase(tables or {})
    monkeypatch.setattr(streaming_worlds, "get_supabase", lambda: db)
    monkeypatch.setattr(streaming_worlds.tmdb, "discover_movies", lambda params: {"results": by_params(params)})
    return db


def test_three_pools_come_back_deduped_against_each_other(app_ctx, monkeypatch):
    """_affinity_genres (derived from the user's own taste signals) is
    exercised separately — this isolates pool-merge/dedup, which is this
    module's own logic."""
    monkeypatch.setattr(streaming_worlds, "_affinity_genres", lambda signals: ([28], [12]))

    def by_params(params):
        # with_genres checked first: a for_you/different call's randomized
        # sort_by can land on "popularity.desc" too (same pool popular draws
        # from), so dispatching on sort_by alone would sometimes misroute a
        # genre-scoped call into popular's branch.
        if params.get("with_genres") == "28":
            return [movie(2), movie(4)]  # 2 overlaps with popular
        if params.get("with_genres") == "12":
            return [movie(5)]
        if params.get("sort_by") == "popularity.desc":
            return [movie(1), movie(2), movie(3)]  # popular — never has with_genres
        return []

    install(monkeypatch, by_params=by_params)
    out = streaming_worlds.get_streaming_world("u1", 8, day=DAY)

    assert [m["id"] for m in out["popular"]] == [1, 2, 3]
    # 2 already claimed by "popular" — for_you keeps only the new one.
    assert 2 not in {m["id"] for m in out["for_you"]}
    assert 4 in {m["id"] for m in out["for_you"]}
    assert {m["id"] for m in out["different"]} == {5}


def test_cold_start_with_no_signals_falls_back_to_a_general_pool(app_ctx, monkeypatch):
    """No reviews/favourites/onboarding genres at all -> for_you/different
    have nothing to filter by, and must not come back empty — distinct
    fallback params return distinct movies so the dedup doesn't erase them."""
    monkeypatch.setattr(streaming_worlds, "_affinity_genres", lambda signals: ([], []))

    def by_params(params):
        if params.get("sort_by") == "popularity.desc" and "page" not in params:
            return [movie(1)]  # popular — never randomized, so never carries a "page" key
        if params.get("sort_by") == "vote_average.desc":
            return [movie(2)]  # for_you fallback — fixed sort, randomized page
        if params.get("sort_by") == "popularity.desc" and "page" in params:
            return [movie(3)]  # different fallback — fixed sort, randomized page
        return []

    install(monkeypatch, by_params=by_params)
    out = streaming_worlds.get_streaming_world("u1", 8, day=DAY)

    assert {m["id"] for m in out["popular"]} == {1}
    assert {m["id"] for m in out["for_you"]} == {2}
    assert {m["id"] for m in out["different"]} == {3}


def test_affinity_genres_ranks_by_weight_and_falls_back_to_onboarding(app_ctx):
    from types import SimpleNamespace

    ranked = SimpleNamespace(genre_affinity={28: 1.0, 12: 5.0, 16: 3.0}, onboarding_genre_ids=[])
    for_you, different = streaming_worlds._affinity_genres(ranked)
    assert for_you == [12, 16, 28]  # highest-weighted first
    assert different == []  # fewer than 4 genres total -> no distinct second tier, not a reused one

    cold = SimpleNamespace(genre_affinity={}, onboarding_genre_ids=[28, 12, 16, 35])
    for_you, different = streaming_worlds._affinity_genres(cold)
    assert for_you == [28, 12, 16]
    assert different == [35]


def test_a_hung_pool_times_out_and_fails_open_to_an_empty_list(app_ctx, monkeypatch):
    import time as time_mod

    def by_params(params):
        if params.get("sort_by") == "popularity.desc" and "page" not in params:
            time_mod.sleep(streaming_worlds._BUDGET_SECONDS + 1)
        return [movie(1)]

    install(monkeypatch, by_params=by_params)
    monkeypatch.setattr(streaming_worlds, "_BUDGET_SECONDS", 0.2)

    started = time_mod.monotonic()
    out = streaming_worlds.get_streaming_world("u1", 8, day=DAY)
    elapsed = time_mod.monotonic() - started

    assert elapsed < streaming_worlds._BUDGET_SECONDS + 2  # didn't block on the hung call
    assert out["popular"] == []  # the hung pool simply comes back empty, not a 500


def test_movie_stubs_are_upserted_for_every_pool(app_ctx, monkeypatch):
    monkeypatch.setattr(streaming_worlds, "_affinity_genres", lambda signals: ([28], []))

    def by_params(params):
        if "with_genres" in params:
            return [movie(2)]
        return [movie(1)]

    db = install(monkeypatch, by_params=by_params)
    streaming_worlds.get_streaming_world("u1", 8, day=DAY)

    stored_ids = {m["id"] for m in db.tables.get("movies", [])}
    assert {1, 2} <= stored_ids


def test_short_pools_are_padded_to_the_target_size_from_a_shared_filler(app_ctx, monkeypatch):
    """Popular already has a full page; For You and Different are each 5
    short. Padding tops both up from the same filler page rather than
    fetching it twice."""
    monkeypatch.setattr(streaming_worlds, "_affinity_genres", lambda signals: ([28], [12]))
    fetch_log = []

    def by_params(params):
        fetch_log.append(dict(params))
        if params.get("with_genres") == "28":
            return [movie(mid) for mid in range(100, 115)]  # 15 — needs 5 more
        if params.get("with_genres") == "12":
            return [movie(mid) for mid in range(200, 215)]  # 15 — needs 5 more
        if params.get("sort_by") == "popularity.desc" and params.get("page", 1) == 1:
            return [movie(mid) for mid in range(1, 21)]  # already 20 — no padding needed
        if params.get("page") == 2:
            return [movie(mid) for mid in range(1000, 1010)]  # exactly enough for both shortfalls combined
        return []

    install(monkeypatch, by_params=by_params)
    out = streaming_worlds.get_streaming_world("u1", 8, day=DAY)

    assert len(out["popular"]) == streaming_worlds.TARGET_POOL_SIZE
    assert len(out["for_you"]) == streaming_worlds.TARGET_POOL_SIZE
    assert len(out["different"]) == streaming_worlds.TARGET_POOL_SIZE
    all_ids = [m["id"] for films in out.values() for m in films]
    assert len(all_ids) == len(set(all_ids))  # no filler movie used to pad more than one section

    filler_fetches = [p for p in fetch_log if p.get("page") == 2 and "with_genres" not in p]
    assert len(filler_fetches) == 1


def test_padding_gives_up_at_its_deadline_rather_than_hang(app_ctx, monkeypatch):
    import time as time_mod

    monkeypatch.setattr(streaming_worlds, "_affinity_genres", lambda signals: ([], []))
    monkeypatch.setattr(streaming_worlds, "_PAD_DEADLINE_SECONDS", 0.2)

    def by_params(params):
        if params.get("sort_by") == "popularity.desc" and params.get("page", 1) == 1:
            return [movie(1)]
        if params.get("page", 0) >= 2:
            time_mod.sleep(0.3)
            return [movie(2)]
        return []

    install(monkeypatch, by_params=by_params)

    started = time_mod.monotonic()
    out = streaming_worlds.get_streaming_world("u1", 8, day=DAY)
    elapsed = time_mod.monotonic() - started

    assert elapsed < 2  # one slow filler fetch, not several — the deadline stopped it from trying more
    assert len(out["popular"]) < streaming_worlds.TARGET_POOL_SIZE  # gave up short rather than hang for it


def test_already_reviewed_films_are_excluded_from_personal_sections_only(app_ctx, monkeypatch):
    """Popular stays a general "what's trending" row (same as Most Popular
    This Week never filtering by personal history) — only For You and
    Different, which are personal picks, drop films the user has already
    reviewed."""
    signals = fake_signals(reviewed_ids={2})
    monkeypatch.setattr(streaming_worlds, "_load_user_signals", lambda user_id, supabase: signals)
    monkeypatch.setattr(streaming_worlds, "_affinity_genres", lambda s: ([28], [12]))

    def by_params(params):
        if params.get("sort_by") == "popularity.desc" and params.get("page", 1) == 1:
            return [movie(1), movie(2), movie(3)]  # movie 2 is "already seen"
        if params.get("with_genres") == "28":
            return [movie(2), movie(4)]  # the For You genre call also turns it up
        if params.get("with_genres") == "12":
            return [movie(2), movie(5)]  # so does Different's
        return []

    install(monkeypatch, by_params=by_params)
    out = streaming_worlds.get_streaming_world("u1", 8, day=DAY)

    assert 2 in {m["id"] for m in out["popular"]}  # Popular isn't filtered by "already seen"
    assert 2 not in {m["id"] for m in out["for_you"]}
    assert 2 not in {m["id"] for m in out["different"]}
    assert 4 in {m["id"] for m in out["for_you"]}
    assert 5 in {m["id"] for m in out["different"]}


def test_padding_also_skips_already_reviewed_films_for_personal_sections(app_ctx, monkeypatch):
    """The shared filler cursor that tops sections up to TARGET_POOL_SIZE
    (see test_short_pools_are_padded...) must apply the same "already seen"
    rule as the main genre-scoped calls when it's filling For You."""
    signals = fake_signals(reviewed_ids={999})
    monkeypatch.setattr(streaming_worlds, "_load_user_signals", lambda user_id, supabase: signals)
    monkeypatch.setattr(streaming_worlds, "_affinity_genres", lambda s: ([28], [12]))

    def by_params(params):
        if params.get("sort_by") == "popularity.desc" and params.get("page", 1) == 1:
            return [movie(mid) for mid in range(1, 21)]  # Popular already full — no padding needed
        if params.get("with_genres") == "28":
            return []  # For You's own genre call comes up empty — padding does all the work
        if params.get("with_genres") == "12":
            # Different gets a handful from its own genre call, so it never
            # hits its own page=2 fallback below — keeps this test isolated
            # to the shared padding filler, which is what's under test.
            return [movie(mid) for mid in range(3000, 3006)]
        if params.get("page") == 2:
            # Filler page: one already-reviewed id (999) mixed in with fresh ones.
            return [movie(999)] + [movie(mid) for mid in range(2000, 2020)]
        return []

    install(monkeypatch, by_params=by_params)
    out = streaming_worlds.get_streaming_world("u1", 8, day=DAY)

    assert 999 not in {m["id"] for m in out["for_you"]}
    assert len(out["for_you"]) == streaming_worlds.TARGET_POOL_SIZE
    # Popular never needed padding (page 1 alone already filled it) — included here
    # mainly to pin down that it's unaffected by any of this, not just For You.
    assert {m["id"] for m in out["popular"]} == set(range(1, 21))


# ─── De-determinizing: two users sharing a genre tuple no longer collide ────

def test_two_users_with_identical_genre_affinity_land_on_different_pages(app_ctx, monkeypatch):
    """The actual bug report: streaming_worlds used to fire one fixed-page,
    fixed-sort discover call for For You/Different, so any two accounts that
    happened to share a top-3 genre ranking (easy via the onboarding
    fallback) got a byte-identical query and byte-identical results. The
    page/sort are now drawn from a per-(user, day, provider, section) seed,
    so two different users asking on the same day land on different pages
    even with the exact same genre tuple."""
    monkeypatch.setattr(streaming_worlds, "_affinity_genres", lambda signals: ([28], []))
    requested_pages = []

    def by_params(params):
        if params.get("with_genres") == "28":
            requested_pages.append(params.get("page"))
            return [movie(mid) for mid in range(1, 21)]
        return []

    install(monkeypatch, by_params=by_params)
    streaming_worlds.get_streaming_world("user-a", 8, day=DAY)
    streaming_worlds.get_streaming_world("user-b", 8, day=DAY)

    assert len(requested_pages) == 2
    assert requested_pages[0] != requested_pages[1]


def test_the_same_user_gets_a_stable_page_across_repeat_requests_on_the_same_day(app_ctx, monkeypatch, no_redis):
    """Reloading the page shouldn't reshuffle it — same reasoning as
    daily_picks' "reloading doesn't reshuffle" guarantee, reused here via
    the same seeded_rng helper. Clears the result cache between calls so
    this proves the underlying seeded_rng stability itself, independent of
    the (separately tested) caching layer short-circuiting a second call
    entirely."""
    monkeypatch.setattr(streaming_worlds, "_affinity_genres", lambda signals: ([28], []))
    requested_pages = []

    def by_params(params):
        if params.get("with_genres") == "28":
            requested_pages.append(params.get("page"))
            return [movie(mid) for mid in range(1, 21)]
        return []

    install(monkeypatch, by_params=by_params)
    streaming_worlds.get_streaming_world("user-a", 8, day=DAY)
    no_redis.clear()
    streaming_worlds.get_streaming_world("user-a", 8, day=DAY)

    assert len(requested_pages) == 2
    assert requested_pages[0] == requested_pages[1]


def test_a_repeat_request_is_served_from_cache_without_recomputing(app_ctx, monkeypatch):
    """The actual point of caching this at all: a second request for the
    same (user, provider, day) must not touch TMDB again."""
    monkeypatch.setattr(streaming_worlds, "_affinity_genres", lambda signals: ([28], []))
    call_count = []

    def by_params(params):
        call_count.append(1)
        return [movie(mid) for mid in range(1, 21)]

    install(monkeypatch, by_params=by_params)
    first = streaming_worlds.get_streaming_world("user-a", 8, day=DAY)
    calls_after_first = len(call_count)
    second = streaming_worlds.get_streaming_world("user-a", 8, day=DAY)

    assert len(call_count) == calls_after_first  # no new TMDB calls on the cache hit
    assert second == first


def test_a_different_provider_is_not_served_from_another_providers_cache(app_ctx, monkeypatch):
    monkeypatch.setattr(streaming_worlds, "_affinity_genres", lambda signals: ([28], []))
    call_count = []
    install(monkeypatch, by_params=lambda params: call_count.append(1) or [movie(mid) for mid in range(1, 21)])

    streaming_worlds.get_streaming_world("user-a", 8, day=DAY)
    calls_after_first = len(call_count)
    streaming_worlds.get_streaming_world("user-a", 9, day=DAY)  # different provider — must recompute, not reuse #8's cache

    assert len(call_count) > calls_after_first


def test_a_different_day_can_land_on_a_different_page_for_the_same_user(app_ctx, monkeypatch):
    monkeypatch.setattr(streaming_worlds, "_affinity_genres", lambda signals: ([28], []))
    requested_pages = []

    def by_params(params):
        if params.get("with_genres") == "28":
            requested_pages.append(params.get("page"))
            return [movie(mid) for mid in range(1, 21)]
        return []

    install(monkeypatch, by_params=by_params)
    streaming_worlds.get_streaming_world("user-a", 8, day=date(2024, 1, 1))
    streaming_worlds.get_streaming_world("user-a", 8, day=date(2024, 6, 15))

    # Not guaranteed to differ for every possible pair of days (the page pool
    # is small), but this specific pair was checked to land on different
    # pages — pins down that `day` actually participates in the seed at all,
    # which a copy-paste bug (e.g. always seeding on today() regardless of
    # the passed-in day) would silently fail.
    assert requested_pages[0] != requested_pages[1]


# ─── Content-aware re-ranking (pure function) ───────────────────────────────

def test_genre_match_score_is_the_fraction_of_target_genres_present():
    assert streaming_worlds._genre_match_score(movie(1, genre_ids=(28, 12)), [28, 12, 16]) == pytest.approx(2 / 3)
    assert streaming_worlds._genre_match_score(movie(1, genre_ids=(99,)), [28, 12, 16]) == 0.0
    assert streaming_worlds._genre_match_score(movie(1, genre_ids=(28,)), []) == 0.0


def test_score_and_order_favours_higher_scoring_movies_without_a_hard_sort(app_ctx):
    import random

    target_genres = [28]
    strong = movie(1, genre_ids=(28,), vote_average=9.0)
    weak = movie(2, genre_ids=(), vote_average=1.0)

    counts = {1: 0, 2: 0}
    for i in range(200):
        ordered = streaming_worlds._score_and_order(
            [strong, weak], target_genres, None, random.Random(f"seed-{i}")
        )
        if ordered[0]["id"] == 1:
            counts[1] += 1
        else:
            counts[2] += 1

    # Not a hard sort (weak sometimes still comes first), but strongly
    # biased toward the higher-scoring movie across many draws.
    assert counts[1] > counts[2]


def test_route_rejects_an_out_of_range_provider_id(monkeypatch):
    from types import SimpleNamespace

    from app.controllers import movies as movies_controller
    from app.utils import auth as auth_utils

    app = create_app()
    app.config.update(TESTING=True, RATELIMIT_ENABLED=False)
    fake_user = SimpleNamespace(id="me-id", factors=[])
    monkeypatch.setattr(auth_utils, "_validate_token", lambda token: fake_user if token == "good" else None)
    called = []
    monkeypatch.setattr(movies_controller.streaming_worlds, "get_streaming_world", lambda *a, **k: called.append(1))

    client = app.test_client()
    resp = client.get("/api/movies/streaming-worlds/999999", headers={"Authorization": "Bearer good"})

    assert resp.status_code == 400
    assert not called  # never reached the service layer with a bogus id


def test_route_passes_through_a_valid_provider_id(monkeypatch):
    from types import SimpleNamespace

    from app.controllers import movies as movies_controller
    from app.utils import auth as auth_utils

    app = create_app()
    app.config.update(TESTING=True, RATELIMIT_ENABLED=False)
    fake_user = SimpleNamespace(id="me-id", factors=[])
    monkeypatch.setattr(auth_utils, "_validate_token", lambda token: fake_user if token == "good" else None)
    seen = {}

    def fake_get(user_id, provider_id):
        seen["user_id"], seen["provider_id"] = user_id, provider_id
        return {"popular": [], "for_you": [], "different": []}

    monkeypatch.setattr(movies_controller.streaming_worlds, "get_streaming_world", fake_get)

    client = app.test_client()
    resp = client.get("/api/movies/streaming-worlds/8", headers={"Authorization": "Bearer good"})

    assert resp.status_code == 200
    assert seen == {"user_id": "me-id", "provider_id": 8}
