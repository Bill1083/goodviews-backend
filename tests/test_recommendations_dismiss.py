"""Dismissing a recommendation ("not interested"), and the stub writes that
used to blank out stored backdrops. PostgREST is faked and records every
write, so the tests can assert on what would reach the database."""
import pytest

from app import create_app
from app.services import recommendations


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    def __init__(self, db, table):
        self.db, self.table = db, table
        self.op, self.payload, self.kwargs = "select", None, {}
        self.filters = []

    # reads
    def select(self, *_a, **_k):
        self.op = "select"
        return self

    def eq(self, col, val):
        self.filters.append((col, val))
        return self

    def in_(self, col, vals):
        self.filters.append((col, set(vals)))
        return self

    def limit(self, _n):
        return self

    def order(self, *_a, **_k):
        return self

    # writes
    def upsert(self, payload, **kwargs):
        self.op, self.payload, self.kwargs = "upsert", payload, kwargs
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def delete(self):
        self.op = "delete"
        return self

    def _matches(self, row):
        for col, val in self.filters:
            if isinstance(val, set):
                if row.get(col) not in val:
                    return False
            elif str(row.get(col)) != str(val):
                return False
        return True

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        self.db.log.append((self.op, self.table, self.payload, self.kwargs, list(self.filters)))
        if self.op == "select":
            return FakeResult([dict(r) for r in rows if self._matches(r)])
        if self.op == "delete":
            self.db.tables[self.table] = [r for r in rows if not self._matches(r)]
            return FakeResult([])
        if self.op == "update":
            for r in rows:
                if self._matches(r):
                    r.update(self.payload)
            return FakeResult([])
        return FakeResult([])


class FakeSupabase:
    def __init__(self, tables):
        self.tables = tables
        self.log = []

    def table(self, name):
        return FakeQuery(self, name)

    def writes(self, op, table):
        return [entry for entry in self.log if entry[0] == op and entry[1] == table]


@pytest.fixture
def app_ctx():
    with create_app().app_context():
        yield


def picks(*movie_ids):
    return [{"user_id": "u1", "items": [{"movie_id": m, "reason": "r"} for m in movie_ids], "computed_at": "2026-10-06T00:00:00+00:00"}]


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


def test_dismissing_a_pick_that_is_not_in_for_you_still_clears_the_picks(app_ctx, monkeypatch):
    db = install(monkeypatch, {"user_weekly_picks": picks(10, 11, 12), "user_recommendations": feed([1, 2, 3])})
    assert recommendations.mark_not_interested("u1", 11) is None
    assert db.tables["user_weekly_picks"] == []  # recomputed on the next fetch
    assert db.writes("upsert", "dismissed_recommendations")


def test_dismissing_a_for_you_film_leaves_unrelated_picks_alone(app_ctx, monkeypatch):
    db = install(monkeypatch, {"user_weekly_picks": picks(10, 11, 12), "user_recommendations": feed([1, 2, 3], overflow=[4])})
    replacement = recommendations.mark_not_interested("u1", 2)
    assert replacement == {"id": 4}
    assert len(db.tables["user_weekly_picks"]) == 1
    assert [it["movie_id"] for it in db.tables["user_recommendations"][0]["items"]] == [1, 3, 4]


def test_dismissing_a_film_in_both_clears_the_picks_and_patches_for_you(app_ctx, monkeypatch):
    db = install(monkeypatch, {"user_weekly_picks": picks(2, 11, 12), "user_recommendations": feed([1, 2, 3], overflow=[4])})
    assert recommendations.mark_not_interested("u1", 2) == {"id": 4}
    assert db.tables["user_weekly_picks"] == []


def test_movie_stubs_are_insert_only(app_ctx, monkeypatch):
    """A stub built without a backdrop must never overwrite one already
    stored — that is how a Pick of the Week lost its hero image."""
    db = install(monkeypatch, {"movies": []})
    monkeypatch.setattr(recommendations.tmdb, "get_top_rated_movies", lambda page=1: {
        "results": [{"id": 7, "title": "T", "poster_path": "/p.jpg", "backdrop_path": None, "release_date": "2001-01-01", "vote_average": 8, "genre_ids": [18]}],
    })
    recommendations._backfill_items(set(), 1, db)
    movie_upserts = db.writes("upsert", "movies")
    assert movie_upserts, "expected the stub write"
    assert all(entry[3].get("ignore_duplicates") is True for entry in movie_upserts)
