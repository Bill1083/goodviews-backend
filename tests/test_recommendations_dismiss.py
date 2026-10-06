"""Dismissing a For You recommendation ("not interested"), and the stub
writes that used to blank out stored backdrops. Taking a film out of Movies of
the Day is covered in test_daily_picks.py."""
import pytest

from app import create_app
from app.services import recommendations
from tests.fakes import FakeSupabase


@pytest.fixture
def app_ctx():
    with create_app().app_context():
        yield


def feed(items, overflow=()):
    return [{
        "user_id": "u1",
        "items": [{"movie_id": m, "reason": "r"} for m in items],
        "overflow": [{"movie_id": m, "reason": "r"} for m in overflow],
    }]


def install(monkeypatch, tables):
    db = FakeSupabase(tables)
    monkeypatch.setattr(recommendations, "get_supabase", lambda: db)
    monkeypatch.setattr(recommendations, "_hydrate", lambda items, sb: {"results": [{"id": it["movie_id"]} for it in items]})
    return db


def test_dismissing_splices_in_the_next_overflow_pick(app_ctx, monkeypatch):
    db = install(monkeypatch, {"user_recommendations": feed([1, 2, 3], overflow=[4])})
    assert recommendations.mark_not_interested("u1", 2) == {"id": 4}
    assert [it["movie_id"] for it in db.tables["user_recommendations"][0]["items"]] == [1, 3, 4]
    assert db.writes("upsert", "dismissed_recommendations")


def test_dismissing_a_film_not_in_for_you_is_still_recorded(app_ctx, monkeypatch):
    db = install(monkeypatch, {"user_recommendations": feed([1, 2, 3])})
    assert recommendations.mark_not_interested("u1", 99) is None
    assert db.writes("upsert", "dismissed_recommendations")
    assert [it["movie_id"] for it in db.tables["user_recommendations"][0]["items"]] == [1, 2, 3]


def test_movie_stubs_are_insert_only(app_ctx, monkeypatch):
    """A stub built without a backdrop must never overwrite one already
    stored — that is how a Pick of the Week lost its hero image."""
    db = install(monkeypatch, {"movies": [{"id": 7, "title": "T", "backdrop_path": "/real.jpg"}]})
    monkeypatch.setattr(recommendations.tmdb, "get_top_rated_movies", lambda page=1: {
        "results": [
            {"id": 7, "title": "T", "poster_path": "/p.jpg", "backdrop_path": None, "release_date": "2001-01-01", "vote_average": 8, "genre_ids": [18]},
            {"id": 8, "title": "U", "poster_path": "/q.jpg", "backdrop_path": "/u.jpg", "release_date": "2002-01-01", "vote_average": 8, "genre_ids": [18]},
        ],
    })
    recommendations._backfill_items(set(), 2, db)
    assert all(entry[3].get("ignore_duplicates") is True for entry in db.writes("upsert", "movies"))
    stored = {m["id"]: m for m in db.tables["movies"]}
    assert stored[7]["backdrop_path"] == "/real.jpg"  # untouched
    assert stored[8]["backdrop_path"] == "/u.jpg"  # new row inserted
