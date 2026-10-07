"""A recommendation must reach each recipient exactly once.

Two distinct bugs lived here:
1. create_review / update_review built the group fan-out and the friend
   fan-out as two separate, un-deduplicated inserts — a friend picked
   directly *and* a member of a selected group got two identical
   notifications. Fixed by merging both into one recipient set.
2. POST /api/movies/recommend had no guard against the same request
   landing twice (e.g. a double-tap) — fixed with a check against an
   already-pending, un-dismissed notification for the same sender+movie.
"""
from types import SimpleNamespace

import pytest

from app import create_app
from app.controllers import movies as movies_controller
from app.controllers import reviews as reviews_controller
from app.utils import auth as auth_utils
from tests.fakes import FakeSupabase

ME = "me-id"
FRIEND = "friend-id"  # both a direct friend AND a member of GROUP below
GROUP = "group-id"

AUTH = {"Authorization": "Bearer good"}


def base_tables():
    return {
        "friendships": [{"user_id": ME, "friend_id": FRIEND}],
        "friend_groups": [{"id": GROUP, "owner_id": ME}],
        "group_members": [{"group_id": GROUP, "user_id": FRIEND}],
        "movies": [],
        "notifications": [],
        "reviews": [],
    }


@pytest.fixture
def client(monkeypatch):
    app = create_app()
    app.config.update(TESTING=True, RATELIMIT_ENABLED=False)

    fake_user = SimpleNamespace(id=ME, factors=[])
    monkeypatch.setattr(auth_utils, "_validate_token", lambda token: fake_user if token == "good" else None)

    # dismissed/is_read default to false at the DB level — the controllers
    # never set them on insert, same as the real notifications table.
    db = FakeSupabase(base_tables(), insert_defaults={"notifications": {"dismissed": False, "is_read": False}})
    monkeypatch.setattr(movies_controller, "get_supabase", lambda: db)
    monkeypatch.setattr(reviews_controller, "get_supabase", lambda: db)

    # Keep review creation deterministic and offline: force the TMDB-backed
    # cache lookup to fail so the controller's own documented fallback (a
    # plain upsert of the client-supplied stub into our fake "movies" table)
    # runs instead, and no-op the cache/daily-picks side effects, which
    # aren't what's under test here.
    monkeypatch.setattr(reviews_controller.movie_cache, "get_movie", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline in tests")))
    monkeypatch.setattr(reviews_controller.cache, "invalidate_user_stats", lambda *_a, **_k: None)
    monkeypatch.setattr(reviews_controller.daily_picks, "drop_from_daily_picks", lambda *_a, **_k: False)

    with app.test_client() as c:
        yield c, db


def notifications_to(db, recipient):
    return [n for n in db.tables["notifications"] if n["user_id"] == recipient]


# ─── POST /api/movies/recommend ──────────────────────────────────────────────

def test_a_friend_selected_directly_and_via_their_group_is_notified_once(client):
    c, db = client
    res = c.post(
        "/api/movies/recommend",
        headers=AUTH,
        json={"movie_id": 1, "title": "Film", "friend_ids": [FRIEND], "group_ids": [GROUP]},
    )
    assert res.status_code == 200
    assert res.get_json()["recipient_count"] == 1
    assert len(notifications_to(db, FRIEND)) == 1


def test_a_repeated_request_does_not_duplicate_the_pending_notification(client):
    c, db = client
    body = {"movie_id": 1, "title": "Film", "friend_ids": [FRIEND]}
    first = c.post("/api/movies/recommend", headers=AUTH, json=body)
    second = c.post("/api/movies/recommend", headers=AUTH, json=body)  # e.g. a double-tap
    assert first.status_code == 200 and second.status_code == 200
    assert second.get_json()["recipient_count"] == 0
    assert len(notifications_to(db, FRIEND)) == 1


def test_a_second_distinct_movie_still_notifies_normally(client):
    """The double-submit guard is scoped to (sender, movie) — recommending a
    different film to the same friend right after must still go through."""
    c, db = client
    c.post("/api/movies/recommend", headers=AUTH, json={"movie_id": 1, "title": "A", "friend_ids": [FRIEND]})
    res = c.post("/api/movies/recommend", headers=AUTH, json={"movie_id": 2, "title": "B", "friend_ids": [FRIEND]})
    assert res.get_json()["recipient_count"] == 1
    assert len(notifications_to(db, FRIEND)) == 2


# ─── POST /api/reviews (create, with friend_ids + group_ids) ────────────────

def test_creating_a_review_notifies_a_friend_in_the_group_once(client):
    c, db = client
    res = c.post(
        "/api/reviews/",
        headers=AUTH,
        json={
            "movie_id": 1, "title": "Film", "rating": 4,
            "friend_ids": [FRIEND], "group_ids": [GROUP],
        },
    )
    assert res.status_code == 201
    assert len(notifications_to(db, FRIEND)) == 1
    # The group share itself is still recorded once per group either way.
    assert len(db.writes("upsert", "group_recommendations")) == 1


# ─── PUT /api/reviews/<id> (update, same friend_ids + group_ids merge) ──────

def test_updating_a_review_notifies_a_friend_in_the_group_once(client):
    c, db = client
    db.tables["reviews"].append({"id": "r1", "user_id": ME, "movie_id": 1, "rating": 3})
    res = c.put(
        f"/api/reviews/r1",
        headers=AUTH,
        json={"friend_ids": [FRIEND], "group_ids": [GROUP]},
    )
    assert res.status_code == 200
    assert len(notifications_to(db, FRIEND)) == 1
