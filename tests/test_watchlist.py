"""POST /api/watchlist/ — in particular, that a film the user has already
reviewed can't land back in the watchlist (reviews.create_review already
removes one from the watchlist on write; this is the other direction)."""
from types import SimpleNamespace

import pytest

from app import create_app
from app.controllers import watchlist as watchlist_controller
from app.utils import auth as auth_utils
from tests.fakes import FakeSupabase

ME = "me-id"
AUTH = {"Authorization": "Bearer good"}


@pytest.fixture
def make_client(monkeypatch):
    def _make(tables: dict | None = None):
        app = create_app()
        app.config.update(TESTING=True, RATELIMIT_ENABLED=False)

        fake_user = SimpleNamespace(id=ME, factors=[])
        monkeypatch.setattr(auth_utils, "_validate_token", lambda token: fake_user if token == "good" else None)

        db = FakeSupabase(tables or {})
        monkeypatch.setattr(watchlist_controller, "get_supabase", lambda: db)
        # Offline in tests — forces the existing fallback-stub path rather
        # than a real TMDB call, same trick test_recommendation_fanout.py uses.
        monkeypatch.setattr(watchlist_controller.movie_cache, "get_movie", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline in tests")))

        return app.test_client(), db

    return _make


def test_requires_auth():
    app = create_app()
    app.config.update(TESTING=True, RATELIMIT_ENABLED=False)
    with app.test_client() as c:
        assert c.post("/api/watchlist/", json={"movie_id": 1}).status_code == 401


def test_adding_a_new_movie_succeeds(make_client):
    client, db = make_client()
    res = client.post("/api/watchlist/", headers=AUTH, json={"movie_id": 1, "title": "Film"})
    assert res.status_code == 201
    assert [w["movie_id"] for w in db.tables["watchlist"]] == [1]


def test_adding_an_already_watchlisted_movie_is_a_clean_no_op(make_client):
    client, db = make_client({"watchlist": [{"user_id": ME, "movie_id": 1}]})
    res = client.post("/api/watchlist/", headers=AUTH, json={"movie_id": 1, "title": "Film"})
    assert res.status_code == 200
    assert len(db.tables["watchlist"]) == 1


def test_adding_an_already_reviewed_movie_is_refused(make_client):
    client, db = make_client({"reviews": [{"id": "r1", "user_id": ME, "movie_id": 1, "rating": 4}]})
    res = client.post("/api/watchlist/", headers=AUTH, json={"movie_id": 1, "title": "Film"})
    assert res.status_code == 409
    assert res.get_json()["already_reviewed"] is True
    # The actual bug: nothing should land in the watchlist table for this movie.
    assert db.tables.get("watchlist", []) == []


def test_someone_elses_review_of_the_same_movie_does_not_block_it(make_client):
    client, db = make_client({"reviews": [{"id": "r1", "user_id": "someone-else", "movie_id": 1, "rating": 4}]})
    res = client.post("/api/watchlist/", headers=AUTH, json={"movie_id": 1, "title": "Film"})
    assert res.status_code == 201
    assert [w["movie_id"] for w in db.tables["watchlist"]] == [1]
