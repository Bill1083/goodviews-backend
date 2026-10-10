"""GET /api/friends/recent-activity — per-friend dedup by movie.

A stray second review row for the same (friend, movie) — e.g. from the
double-submit create_review now guards against — used to render as two
back-to-back identical tiles. These tests pin down the fix: one row per
(friend, movie), keeping whichever came first in the (already created_at-desc
ordered) query result, while still showing the SAME movie once per DIFFERENT
friend who reviewed it (that's legitimate, not a duplicate)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import create_app
from app.controllers import friends as friends_controller
from app.utils import auth as auth_utils
from tests.fakes import FakeSupabase

ME = "me-id"
FRIEND1 = "friend-1"
FRIEND2 = "friend-2"
AUTH = {"Authorization": "Bearer good"}


def friendship(friend_id, username, hide=False):
    return {
        "user_id": ME,
        "friend_id": friend_id,
        "profiles": {"id": friend_id, "username": username, "hide_recent_movies": hide},
    }


def recent(hours_ago: float) -> str:
    """Within the endpoint's 7-day window, relative to "now" — a hardcoded
    past date would fall outside that window once enough real time passes."""
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def review(rid, user_id, movie_id, hours_ago, title="Film"):
    return {
        "id": rid,
        "user_id": user_id,
        "movie_id": movie_id,
        "rating": 4,
        "review_text": "",
        "rewatch_count": 0,
        "created_at": recent(hours_ago),
        "is_onboarding": False,
        "movies": {"id": movie_id, "title": title, "poster_path": None, "release_date": "2020-01-01"},
    }


@pytest.fixture
def client(monkeypatch):
    app = create_app()
    app.config.update(TESTING=True, RATELIMIT_ENABLED=False)

    fake_user = SimpleNamespace(id=ME, factors=[])
    monkeypatch.setattr(auth_utils, "_validate_token", lambda token: fake_user if token == "good" else None)

    def make(tables):
        db = FakeSupabase(tables)
        monkeypatch.setattr(friends_controller, "get_supabase", lambda: db)
        return db

    with app.test_client() as c:
        yield c, make


def test_two_reviews_for_the_same_friend_and_movie_collapse_to_one_tile(client):
    c, make = client
    # Query results aren't sorted by the fake (order() is a no-op there), so
    # seed already in the created_at-desc order the real query would return.
    make({
        "friendships": [friendship(FRIEND1, "alice")],
        "reviews": [
            review("r2", FRIEND1, 42, 1),
            review("r1", FRIEND1, 42, 2),
        ],
    })
    resp = c.get("/api/friends/recent-activity", headers=AUTH)
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data) == 1
    reviews = data[0]["reviews"]
    assert len(reviews) == 1
    assert reviews[0]["id"] == "r2"  # the one seeded first = most recent


def test_different_friends_reviewing_the_same_movie_both_still_appear(client):
    c, make = client
    make({
        "friendships": [friendship(FRIEND1, "alice"), friendship(FRIEND2, "bob")],
        "reviews": [
            review("r1", FRIEND1, 42, 2),
            review("r2", FRIEND2, 42, 1),
        ],
    })
    resp = c.get("/api/friends/recent-activity", headers=AUTH)
    assert resp.status_code == 200
    data = resp.get_json()
    by_friend = {d["friend_id"]: d for d in data}
    assert set(by_friend) == {FRIEND1, FRIEND2}
    assert [r["movie_id"] for r in by_friend[FRIEND1]["reviews"]] == [42]
    assert [r["movie_id"] for r in by_friend[FRIEND2]["reviews"]] == [42]


def test_a_single_friend_reviewing_two_different_movies_keeps_both(client):
    c, make = client
    make({
        "friendships": [friendship(FRIEND1, "alice")],
        "reviews": [
            review("r1", FRIEND1, 1, 1),
            review("r2", FRIEND1, 2, 2),
        ],
    })
    resp = c.get("/api/friends/recent-activity", headers=AUTH)
    data = resp.get_json()
    assert sorted(r["movie_id"] for r in data[0]["reviews"]) == [1, 2]
