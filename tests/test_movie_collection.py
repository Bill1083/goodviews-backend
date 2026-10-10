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


@pytest.fixture(autouse=True)
def no_redis(monkeypatch):
    """The franchise-fallback cache is a pure optimization (see
    movie_cache._fallback_franchise_collection) — tests exercise the
    uncached path deterministically rather than depending on whether a real
    Redis happens to be reachable in this environment."""
    store: dict = {}
    monkeypatch.setattr(movie_cache.cache, "cache_get", lambda key: store.get(key))
    monkeypatch.setattr(movie_cache.cache, "cache_set", lambda key, value, ttl=None: store.__setitem__(key, value))
    return store


def test_a_standalone_film_falls_back_to_recommendations_and_finds_nothing(client, monkeypatch):
    """No official TMDB collection AND nothing in its recommendations
    shares its title — the overwhelmingly common case. One extra TMDB
    call (recommendations), still ends at {"collection": None}. core_updated_at
    set, so this is a row that's already been properly fetched (not a bare
    stub) — see the dedicated stub-row test below for that distinction."""
    c, make = client
    make({"movies": [{"id": 1, "title": "A Standalone Film", "collection_id": None, "collection_name": None, "core_updated_at": "2026-01-01T00:00:00+00:00"}]})

    calls = []

    def fake_recommendations(movie_id):
        calls.append(movie_id)
        return {"results": [{"id": 999, "title": "Some Totally Unrelated Film"}]}

    monkeypatch.setattr(movie_cache.tmdb, "get_movie_recommendations", fake_recommendations)
    called = []
    monkeypatch.setattr(movie_cache.tmdb, "get_collection_details", lambda cid: called.append(cid) or {})

    resp = c.get("/api/movies/1/collection")
    assert resp.status_code == 200
    assert resp.get_json() == {"collection": None}
    assert calls == [1]
    assert not called  # never an official-collection lookup when there's no collection_id


def test_a_franchise_with_no_official_collection_falls_back_to_filtered_recommendations(client, monkeypatch):
    """The actual reported case: Spider-Man: Homecoming has no TMDB
    collection, but its own recommendations include No Way Home."""
    c, make = client
    make({"movies": [{"id": 315635, "title": "Spider-Man: Homecoming", "collection_id": None, "collection_name": None, "core_updated_at": "2026-01-01T00:00:00+00:00"}]})

    def fake_recommendations(movie_id):
        return {
            "results": [
                {"id": 634649, "title": "Spider-Man: No Way Home", "poster_path": "/p.jpg", "release_date": "2021-12-15", "vote_average": 8.0, "genre_ids": [28]},
                {"id": 24428, "title": "The Avengers", "poster_path": "/a.jpg", "release_date": "2012-04-25", "vote_average": 7.7, "genre_ids": [28]},
            ]
        }

    monkeypatch.setattr(movie_cache.tmdb, "get_movie_recommendations", fake_recommendations)

    resp = c.get("/api/movies/315635/collection")
    assert resp.status_code == 200
    collection = resp.get_json()["collection"]
    assert collection["id"] is None
    assert collection["name"] == "Spider-Man"
    assert [p["id"] for p in collection["parts"]] == [634649]


def test_the_franchise_fallback_result_is_cached_per_movie(client, monkeypatch, no_redis):
    c, make = client
    make({"movies": [{"id": 1, "title": "A Standalone Film", "collection_id": None, "collection_name": None, "core_updated_at": "2026-01-01T00:00:00+00:00"}]})

    calls = []
    monkeypatch.setattr(movie_cache.tmdb, "get_movie_recommendations", lambda mid: calls.append(mid) or {"results": []})

    c.get("/api/movies/1/collection")
    c.get("/api/movies/1/collection")

    assert calls == [1]  # second request served from cache, not a second TMDB call


def test_a_movie_with_a_collection_returns_ordered_parts(client, monkeypatch):
    c, make = client
    make({"movies": [{"id": 2, "title": "Kung Fu Panda 2", "collection_id": 99, "collection_name": "Kung Fu Panda Collection", "core_updated_at": "2026-01-01T00:00:00+00:00"}]})

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
    make({"movies": [{"id": 3, "title": "Film", "collection_id": 7, "collection_name": "A Collection", "core_updated_at": "2026-01-01T00:00:00+00:00"}]})

    def boom(cid):
        raise RuntimeError("TMDB unreachable")

    monkeypatch.setattr(movie_cache.tmdb, "get_collection_details", boom)

    resp = c.get("/api/movies/3/collection")
    assert resp.status_code == 502


def test_a_cold_id_not_in_the_movies_table_fetches_core_first(client, monkeypatch):
    c, make = client
    db = make({"movies": []})

    def fake_get_movie(movie_id, segments=None):
        db.tables["movies"].append({
            "id": movie_id, "title": "Newly Seen Film", "collection_id": None, "collection_name": None,
            "core_updated_at": "2026-01-01T00:00:00+00:00",
        })
        return {"id": movie_id, "title": "Newly Seen Film"}

    monkeypatch.setattr(movie_cache, "get_movie", fake_get_movie)
    called = []
    monkeypatch.setattr(movie_cache.tmdb, "get_collection_details", lambda cid: called.append(cid) or {})
    monkeypatch.setattr(movie_cache.tmdb, "get_movie_recommendations", lambda mid: {"results": []})

    resp = c.get("/api/movies/4/collection")
    assert resp.status_code == 200
    assert resp.get_json() == {"collection": None}
    assert not called


def test_a_stub_row_with_no_collection_id_is_refetched_before_trusting_it(client, monkeypatch):
    """The actual reported case: "The Dark Knight" genuinely has an
    official TMDB collection, but a hugely popular movie like it is very
    likely to already have a bare candidate-pool stub row (no
    collection_id, no core_updated_at — see recommendations._upsert_movie_stub)
    from someone else's For You/streaming-world feed, written long before
    anyone opens its own detail modal. A NULL collection_id on a row like
    that must not be trusted as "no franchise" — it must trigger a real
    fetch first, same as a row that doesn't exist at all."""
    c, make = client
    db = make({"movies": [{"id": 155, "title": "The Dark Knight", "collection_id": None, "collection_name": None}]})

    def fake_get_movie(movie_id, segments=None):
        for row in db.tables["movies"]:
            if row["id"] == movie_id:
                row.update({"collection_id": 263, "collection_name": "The Dark Knight Collection", "core_updated_at": "2026-01-01T00:00:00+00:00"})
        return {"id": movie_id}

    monkeypatch.setattr(movie_cache, "get_movie", fake_get_movie)

    def fake_collection_details(cid):
        assert cid == 263
        return {
            "id": 263, "name": "The Dark Knight Collection", "poster_path": None, "backdrop_path": None,
            "parts": [{"id": 49026, "title": "The Dark Knight Rises", "release_date": "2012-07-17", "poster_path": "/p.jpg", "vote_average": 7.8, "genre_ids": [28]}],
        }

    monkeypatch.setattr(movie_cache.tmdb, "get_collection_details", fake_collection_details)
    recommendations_called = []
    monkeypatch.setattr(movie_cache.tmdb, "get_movie_recommendations", lambda mid: recommendations_called.append(mid) or {"results": []})

    resp = c.get("/api/movies/155/collection")
    assert resp.status_code == 200
    collection = resp.get_json()["collection"]
    assert collection["id"] == 263
    assert [p["id"] for p in collection["parts"]] == [49026]
    assert not recommendations_called  # found the real collection — never needed the fallback at all
