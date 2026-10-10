"""POST /api/reviews/ — a second submission for the same (user, movie) must
edit the existing review row, not insert a second one.

Without this guard, a double-submit (double-tap, a retried request) leaves
two review rows for the same movie, which then surfaced as duplicate tiles
in a friend's "recently watched" feed (see test_friends_recent_activity.py
for the read-side half of this same fix)."""
from types import SimpleNamespace

import pytest

from app import create_app
from app.controllers import reviews as reviews_controller
from app.utils import auth as auth_utils
from tests.fakes import FakeSupabase

ME = "me-id"
FRIEND = "friend-id"
AUTH = {"Authorization": "Bearer good"}


def base_tables():
    return {
        "friendships": [{"user_id": ME, "friend_id": FRIEND}],
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

    db = FakeSupabase(base_tables(), insert_defaults={"notifications": {"dismissed": False, "is_read": False}})
    monkeypatch.setattr(reviews_controller, "get_supabase", lambda: db)

    monkeypatch.setattr(reviews_controller.movie_cache, "get_movie", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline in tests")))
    monkeypatch.setattr(reviews_controller.cache, "invalidate_user_stats", lambda *_a, **_k: None)
    monkeypatch.setattr(reviews_controller.daily_picks, "drop_from_daily_picks", lambda *_a, **_k: False)

    with app.test_client() as c:
        yield c, db


def notifications_to(db, recipient):
    return [n for n in db.tables["notifications"] if n["user_id"] == recipient]


def test_a_repeated_review_for_the_same_movie_updates_in_place(client):
    c, db = client
    body = {"movie_id": 1, "title": "Film", "rating": 3, "review_text": "meh"}
    first = c.post("/api/reviews/", headers=AUTH, json=body)
    assert first.status_code == 201
    first_id = first.get_json()["id"]

    second = c.post("/api/reviews/", headers=AUTH, json={**body, "rating": 5, "review_text": "actually great"})
    assert second.status_code == 200
    second_json = second.get_json()
    assert second_json["id"] == first_id
    assert second_json["rating"] == 5
    assert second_json["review_text"] == "actually great"

    movie_reviews = [r for r in db.tables["reviews"] if r["user_id"] == ME and r["movie_id"] == 1]
    assert len(movie_reviews) == 1
    assert movie_reviews[0]["rating"] == 5


def test_the_repeated_submission_does_not_re_notify_friends(client):
    c, db = client
    body = {"movie_id": 1, "title": "Film", "rating": 4, "friend_ids": [FRIEND]}
    c.post("/api/reviews/", headers=AUTH, json=body)
    c.post("/api/reviews/", headers=AUTH, json=body)  # e.g. a double-tap
    assert len(notifications_to(db, FRIEND)) == 1


def test_a_second_distinct_movie_still_inserts_normally(client):
    c, db = client
    c.post("/api/reviews/", headers=AUTH, json={"movie_id": 1, "title": "A", "rating": 4})
    res = c.post("/api/reviews/", headers=AUTH, json={"movie_id": 2, "title": "B", "rating": 4})
    assert res.status_code == 201

    my_reviews = [r for r in db.tables["reviews"] if r["user_id"] == ME]
    assert sorted(r["movie_id"] for r in my_reviews) == [1, 2]


def test_onboarding_reviews_are_not_deduped_against_a_normal_review(client):
    """Onboarding quick-ratings are a distinct, one-time import — a later
    genuine review of the same movie must still insert its own row rather
    than silently overwrite the onboarding one."""
    c, db = client
    c.post("/api/reviews/", headers=AUTH, json={"movie_id": 1, "title": "A", "rating": 3, "is_onboarding": True})
    res = c.post("/api/reviews/", headers=AUTH, json={"movie_id": 1, "title": "A", "rating": 5})
    assert res.status_code == 201

    my_reviews = [r for r in db.tables["reviews"] if r["user_id"] == ME and r["movie_id"] == 1]
    assert len(my_reviews) == 2
