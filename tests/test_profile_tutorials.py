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
}

AUTH = {"Authorization": "Bearer good"}


class FakeProfileTable:
    """Just enough of the fluent PostgREST builder for profile.py's self
    routes: select/update + eq/single + execute. `missing` simulates a
    column sql/011 hasn't added yet by raising the same shape of error
    Postgres gives for an unknown column."""

    def __init__(self, row: dict, missing: set[str] = frozenset()):
        self.row = dict(row)
        self.missing = missing
        self._cols: list[str] | None = None
        self._single = False
        self._update_values: dict | None = None

    def select(self, cols: str, **_kw):
        self._cols = [c.strip() for c in cols.split(",")]
        self._update_values = None
        return self

    def update(self, values: dict):
        self._update_values = values
        self._cols = None
        return self

    def eq(self, _col, _val):
        return self

    def single(self):
        self._single = True
        return self

    def execute(self):
        if self._update_values is not None:
            self.row.update(self._update_values)
            return SimpleNamespace(data=[dict(self.row)])
        missing = [c for c in (self._cols or []) if c in self.missing]
        if missing:
            raise RuntimeError(f"column profiles.{missing[0]} does not exist")
        projected = {c: self.row.get(c) for c in (self._cols or [])}
        return SimpleNamespace(data=projected if self._single else [projected])


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
