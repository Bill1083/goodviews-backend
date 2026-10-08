"""One streaming service's own Discover sub-page (Popular/For You/Different),
scoped via TMDB's own with_watch_providers rather than a per-movie
availability bolt-on — see app.services.streaming_worlds' module docstring
for why that sidesteps the class of timeout bug the main streaming filter
needed several rounds to fix."""
import pytest

from app import create_app
from app.services import streaming_worlds
from tests.fakes import FakeSupabase


@pytest.fixture
def app_ctx():
    with create_app().app_context():
        yield


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
        if params.get("sort_by") == "popularity.desc":
            return [movie(1), movie(2), movie(3)]
        if params.get("with_genres") == "28":
            return [movie(2), movie(4)]  # 2 overlaps with popular
        if params.get("with_genres") == "12":
            return [movie(5)]
        return []

    install(monkeypatch, by_params=by_params)
    out = streaming_worlds.get_streaming_world("u1", 8)

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
            return [movie(1)]
        if params.get("sort_by") == "vote_average.desc":
            return [movie(2)]
        if params.get("page") == 2:
            return [movie(3)]
        return []

    install(monkeypatch, by_params=by_params)
    out = streaming_worlds.get_streaming_world("u1", 8)

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
    out = streaming_worlds.get_streaming_world("u1", 8)
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
    streaming_worlds.get_streaming_world("u1", 8)

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
    out = streaming_worlds.get_streaming_world("u1", 8)

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
    out = streaming_worlds.get_streaming_world("u1", 8)
    elapsed = time_mod.monotonic() - started

    assert elapsed < 2  # one slow filler fetch, not several — the deadline stopped it from trying more
    assert len(out["popular"]) < streaming_worlds.TARGET_POOL_SIZE  # gave up short rather than hang for it


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
