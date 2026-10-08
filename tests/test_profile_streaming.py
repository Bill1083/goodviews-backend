"""The self-profile support for streaming-service selection: GET /api/profile/
returning streaming_provider_ids + streaming_filter_enabled, and PUT
/api/profile/ validating and saving them — including falling back gracefully
before sql/012_streaming_services.sql / sql/013_streaming_filter_toggle.sql
have been applied.
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
    "streaming_filter_enabled": False,
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


def test_self_profile_includes_streaming_provider_ids(make_client):
    row = dict(BASE_ROW, streaming_provider_ids=[8, 337])
    client, _ = make_client(row)
    body = client.get("/api/profile/", headers=AUTH).get_json()
    assert body["streaming_provider_ids"] == [8, 337]


def test_self_profile_falls_back_before_the_migration(make_client):
    """sql/012 not applied yet — everything else still loads."""
    client, _ = make_client(BASE_ROW, missing={"streaming_provider_ids"})
    res = client.get("/api/profile/", headers=AUTH)
    assert res.status_code == 200
    body = res.get_json()
    assert "streaming_provider_ids" not in body
    assert body["username"] == "me"


def test_self_profile_falls_back_when_both_new_columns_are_missing(make_client):
    """A deploy stale enough to predate sql/011 *and* sql/012 should still
    serve a usable profile, not just the single-column fallback."""
    client, _ = make_client(BASE_ROW, missing={"streaming_provider_ids", "seen_tutorials"})
    res = client.get("/api/profile/", headers=AUTH)
    assert res.status_code == 200
    body = res.get_json()
    assert "streaming_provider_ids" not in body
    assert "seen_tutorials" not in body
    assert body["username"] == "me"


def test_saving_streaming_provider_ids(make_client):
    client, table = make_client(BASE_ROW)
    res = client.put("/api/profile/", headers=AUTH, json={"streaming_provider_ids": [8, 337, 119]})
    assert res.status_code == 200
    assert res.get_json()["streaming_provider_ids"] == [8, 337, 119]
    assert table.row["streaming_provider_ids"] == [8, 337, 119]


def test_saving_an_empty_list_clears_it(make_client):
    row = dict(BASE_ROW, streaming_provider_ids=[8])
    client, table = make_client(row)
    res = client.put("/api/profile/", headers=AUTH, json={"streaming_provider_ids": []})
    assert res.status_code == 200
    assert table.row["streaming_provider_ids"] == []


@pytest.mark.parametrize("bad", [["not-a-number"], [-1], [0], [999999999]])
def test_rejects_invalid_provider_ids(make_client, bad):
    client, _ = make_client(BASE_ROW)
    res = client.put("/api/profile/", headers=AUTH, json={"streaming_provider_ids": bad})
    assert res.status_code == 400


def test_rejects_a_non_list_value(make_client):
    client, _ = make_client(BASE_ROW)
    res = client.put("/api/profile/", headers=AUTH, json={"streaming_provider_ids": "netflix"})
    assert res.status_code == 400


# ─── streaming_filter_enabled ─────────────────────────────────────────────────

def test_self_profile_includes_streaming_filter_enabled(make_client):
    row = dict(BASE_ROW, streaming_filter_enabled=True)
    client, _ = make_client(row)
    body = client.get("/api/profile/", headers=AUTH).get_json()
    assert body["streaming_filter_enabled"] is True


def test_self_profile_falls_back_when_only_the_toggle_column_is_missing(make_client):
    """Keeping provider ids selectable even if sql/013 hasn't landed yet —
    only the toggle itself is missing, not the whole streaming feature."""
    client, _ = make_client(dict(BASE_ROW, streaming_provider_ids=[8]), missing={"streaming_filter_enabled"})
    res = client.get("/api/profile/", headers=AUTH)
    assert res.status_code == 200
    body = res.get_json()
    assert "streaming_filter_enabled" not in body
    assert body["streaming_provider_ids"] == [8]


def test_saving_streaming_filter_enabled(make_client):
    client, table = make_client(BASE_ROW)
    res = client.put("/api/profile/", headers=AUTH, json={"streaming_filter_enabled": True})
    assert res.status_code == 200
    assert res.get_json()["streaming_filter_enabled"] is True
    assert table.row["streaming_filter_enabled"] is True


def test_saving_streaming_filter_enabled_coerces_truthy_values(make_client):
    client, table = make_client(BASE_ROW)
    client.put("/api/profile/", headers=AUTH, json={"streaming_filter_enabled": 1})
    assert table.row["streaming_filter_enabled"] is True
