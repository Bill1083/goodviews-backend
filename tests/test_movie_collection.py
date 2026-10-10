"""GET /api/movies/<id>/collection — the franchise "Universe" row's data
source. No-collection is a zero-TMDB-call path; a TMDB failure degrades to
502, never an unhandled 500."""
import pytest

from app import create_app
from app.services import movie_cache
from tests.fakes import FakeSupabase


@pytest.fixture
def client(monkeypatch):
    app = create_app()
    app.config.update(TESTING=True, RATELIMIT_ENABLED=False)

    def make(tables):
        db = FakeSupabase(tables)
        monkeypatch.setattr(movie_cache, "get_supabase", lambda: db)
        return db

    with app.test_client() as c:
        yield c, make


def test_a_movie_with_no_collection_returns_null_with_no_tmdb_call(client, monkeypatch):
    c, make = client
    make({"movies": [{"id": 1, "title": "A Standalone Film", "collection_id": None, "collection_name": None}]})

    called = []
    monkeypatch.setattr(movie_cache.tmdb, "get_collection_details", lambda cid: called.append(cid) or {})

    resp = c.get("/api/movies/1/collection")
    assert resp.status_code == 200
    assert resp.get_json() == {"collection": None}
    assert not called


def test_a_movie_with_a_collection_returns_ordered_parts(client, monkeypatch):
    c, make = client
    make({"movies": [{"id": 2, "title": "Kung Fu Panda 2", "collection_id": 99, "collection_name": "Kung Fu Panda Collection"}]})

    def fake_collection_details(cid):
        assert cid == 99
        return {
            "id": 99,
            "name": "Kung Fu Panda Collection",
            "poster_path": "/poster.jpg",
            "backdrop_path": "/backdrop.jpg",
            "parts": [
                {"id": 3, "title": "Kung Fu Panda 3", "poster_path": "/3.jpg", "release_date": "2016-01-29", "vote_average": 7.1, "genre_ids": [16]},
                {"id": 1, "title": "Kung Fu Panda", "poster_path": "/1.jpg", "release_date": "2008-06-06", "vote_average": 7.2, "genre_ids": [16]},
                {"id": 2, "title": "Kung Fu Panda 2", "poster_path": "/2.jpg", "release_date": "2011-05-26", "vote_average": 7.0, "genre_ids": [16]},  # the seed itself
                {"id": 50, "title": "Kung Fu Panda: Secrets of the Scroll", "poster_path": "/50.jpg", "release_date": "2016-01-23", "vote_average": 6.0, "genre_ids": [16]},
            ],
        }

    monkeypatch.setattr(movie_cache.tmdb, "get_collection_details", fake_collection_details)

    resp = c.get("/api/movies/2/collection")
    assert resp.status_code == 200
    data = resp.get_json()
    collection = data["collection"]
    assert collection["id"] == 99
    assert collection["name"] == "Kung Fu Panda Collection"
    # The seed movie (2) is excluded from its own "other movies in this
    # franchise" row; the two numbered sequels (1, 3) sort before the short
    # (50), each in release-date order.
    assert [p["id"] for p in collection["parts"]] == [1, 3, 50]


def test_a_tmdb_failure_degrades_to_502_not_an_unhandled_500(client, monkeypatch):
    c, make = client
    make({"movies": [{"id": 3, "title": "Film", "collection_id": 7, "collection_name": "A Collection"}]})

    def boom(cid):
        raise RuntimeError("TMDB unreachable")

    monkeypatch.setattr(movie_cache.tmdb, "get_collection_details", boom)

    resp = c.get("/api/movies/3/collection")
    assert resp.status_code == 502


def test_a_cold_id_not_in_the_movies_table_fetches_core_first(client, monkeypatch):
    c, make = client
    db = make({"movies": []})

    def fake_get_movie(movie_id, segments=None):
        db.tables["movies"].append({"id": movie_id, "title": "Newly Seen Film", "collection_id": None, "collection_name": None})
        return {"id": movie_id, "title": "Newly Seen Film"}

    monkeypatch.setattr(movie_cache, "get_movie", fake_get_movie)
    called = []
    monkeypatch.setattr(movie_cache.tmdb, "get_collection_details", lambda cid: called.append(cid) or {})

    resp = c.get("/api/movies/4/collection")
    assert resp.status_code == 200
    assert resp.get_json() == {"collection": None}
    assert not called
