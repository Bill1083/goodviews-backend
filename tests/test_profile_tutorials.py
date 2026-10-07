"""The self-profile support for in-app feature tutorials: GET /api/profile/
returning seen_tutorials + created_at, and PUT /api/profile/ appending a key
via seen_tutorial — including both falling back gracefully before
sql/011_feature_tutorials.sql has been applied, since that's meant to be
safe to deploy ahead of the migration.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app import create_app
from app.controllers import profile as profile_controller
from app.utils import auth as auth_utils
from tests.fakes import FakeProfileTable

ME = "me-id"
CREATED_AT = datetime(2025, 1, 1, tzinfo=timezone.utc)

BASE_ROW = {
    "id": ME,
    "username": "me",
    "bio": None,
    "profile_visibility": "friends_only",
    "avatar_color": None,
    "avatar_url": None,
    "avatar_focal_y": 50,
    "avatar_zoom": 1.0,
    "hide_recent_movies": False,
    "hide_friends_list": False,
    "mute_recommendations": False,
    "mute_friend_requests": False,
    "has_onboarded": True,
    "onboarding_genre_ids": [],
    "seen_tutorials": [],
    "streaming_provider_ids": [],
}

AUTH = {"Authorization": "Bearer good"}


@pytest.fixture
def make_client(monkeypatch):
    def _make(row: dict, missing: set[str] = frozenset()):
        app = create_app()
        app.config.update(TESTING=True, RATELIMIT_ENABLED=False)

        fake_user = SimpleNamespace(id=row["id"], factors=[], created_at=CREATED_AT)
        monkeypatch.setattr(auth_utils, "_validate_token", lambda token: fake_user if token == "good" else None)

        table = FakeProfileTable(row, missing)
        monkeypatch.setattr(profile_controller, "get_supabase", lambda: SimpleNamespace(table=lambda _name: table))

        return app.test_client(), table

    return _make


def test_self_profile_includes_seen_tutorials_and_created_at(make_client):
    client, _ = make_client(BASE_ROW)
    body = client.get("/api/profile/", headers=AUTH).get_json()
    assert body["seen_tutorials"] == []
    assert body["created_at"] == "2025-01-01T00:00:00+00:00"


def test_self_profile_falls_back_before_the_migration(make_client):
    """sql/011 not applied yet — the profile still loads, just without the
    new field. created_at still comes through, since it's never a DB column."""
    client, _ = make_client(BASE_ROW, missing={"seen_tutorials"})
    res = client.get("/api/profile/", headers=AUTH)
    assert res.status_code == 200
    body = res.get_json()
    assert "seen_tutorials" not in body
    assert body["username"] == "me"
    assert body["created_at"] == "2025-01-01T00:00:00+00:00"


def test_marking_a_tutorial_seen_appends_the_key(make_client):
    client, _ = make_client(BASE_ROW)
    res = client.put("/api/profile/", headers=AUTH, json={"seen_tutorial": "hover_highlight"})
    assert res.status_code == 200
    assert res.get_json()["seen_tutorials"] == ["hover_highlight"]


def test_marking_an_already_seen_tutorial_does_not_duplicate_or_error(make_client):
    row = dict(BASE_ROW, seen_tutorials=["hover_highlight"])
    client, table = make_client(row)
    res = client.put("/api/profile/", headers=AUTH, json={"seen_tutorial": "hover_highlight"})
    # Nothing changed, so this is the same "no valid fields" response any
    # other no-op update gets — not a crash, and not a duplicate entry.
    assert res.status_code == 400
    assert table.row["seen_tutorials"] == ["hover_highlight"]


def test_marking_seen_before_the_migration_is_a_clean_no_op(make_client):
    client, _ = make_client(BASE_ROW, missing={"seen_tutorials"})
    res = client.put("/api/profile/", headers=AUTH, json={"seen_tutorial": "hover_highlight"})
    assert res.status_code == 400
    assert "error" in res.get_json()
