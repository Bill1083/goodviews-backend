"""GET /api/movies/for-you and /movies-of-the-day read the caller's
streaming-filter prefs and pass them to the streaming-aware feed functions;
Most Popular This Week never does — it isn't even user-aware (no
@require_auth), so there's no per-user preference to apply there at all.

The backfill behaviour itself (restoring the count after filtering) is
covered by tests/test_streaming_backfill.py — this file is purely about the
controller wiring: does the route read the right prefs and pass them through.
"""
import inspect
from types import SimpleNamespace

import pytest

from app import create_app
from app.controllers import movies as movies_controller
from app.utils import auth as auth_utils
from tests.fakes import FakeProfileTable

ME = "me-id"
AUTH = {"Authorization": "Bearer good"}


@pytest.fixture
def make_client(monkeypatch):
    def _make(profile_row: dict, missing: set[str] = frozenset()):
        app = create_app()
        app.config.update(TESTING=True, RATELIMIT_ENABLED=False)

        fake_user = SimpleNamespace(id=ME, factors=[])
        monkeypatch.setattr(auth_utils, "_validate_token", lambda token: fake_user if token == "good" else None)

        table = FakeProfileTable(profile_row, missing)
        monkeypatch.setattr(movies_controller, "get_supabase", lambda: SimpleNamespace(table=lambda _name: table))

        seen = {}

        def fake_for_you(user_id, provider_ids, force=False):
            seen["for_you_provider_ids"] = provider_ids
            return {"page": 1, "total_pages": 1, "total_results": 0, "results": []}

        def fake_daily(user_id, tz, provider_ids, force=False):
            seen["daily_provider_ids"] = provider_ids
            return {"page": 1, "total_pages": 1, "total_results": 0, "results": []}

        monkeypatch.setattr(movies_controller.recommendations, "get_recommendations_for_user_streaming", fake_for_you)
        monkeypatch.setattr(movies_controller.daily_picks, "get_daily_picks_streaming", fake_daily)

        return app.test_client(), seen

    return _make


def test_for_you_passes_provider_ids_when_the_toggle_is_on(make_client):
    row = {"id": ME, "streaming_filter_enabled": True, "streaming_provider_ids": [8, 337]}
    client, seen = make_client(row)
    assert client.get("/api/movies/for-you", headers=AUTH).status_code == 200
    assert seen["for_you_provider_ids"] == [8, 337]


def test_for_you_passes_no_providers_when_the_toggle_is_off(make_client):
    row = {"id": ME, "streaming_filter_enabled": False, "streaming_provider_ids": [8, 337]}
    client, seen = make_client(row)
    assert client.get("/api/movies/for-you", headers=AUTH).status_code == 200
    assert seen["for_you_provider_ids"] == []


def test_movies_of_the_day_passes_provider_ids_when_the_toggle_is_on(make_client):
    row = {"id": ME, "streaming_filter_enabled": True, "streaming_provider_ids": [21]}
    client, seen = make_client(row)
    assert client.get("/api/movies/movies-of-the-day?tz=UTC", headers=AUTH).status_code == 200
    assert seen["daily_provider_ids"] == [21]


def test_picks_of_the_week_passes_provider_ids_too(make_client):
    row = {"id": ME, "streaming_filter_enabled": True, "streaming_provider_ids": [21]}
    client, seen = make_client(row)
    assert client.get("/api/movies/picks-of-the-week", headers=AUTH).status_code == 200
    assert seen["daily_provider_ids"] == [21]


def test_filter_prefs_missing_columns_read_as_off(make_client):
    """sql/012/013 not applied yet — no crash, just unfiltered."""
    row = {"id": ME, "streaming_filter_enabled": False, "streaming_provider_ids": []}
    client, seen = make_client(row, missing={"streaming_filter_enabled", "streaming_provider_ids"})
    assert client.get("/api/movies/for-you", headers=AUTH).status_code == 200
    assert seen["for_you_provider_ids"] == []


def test_trending_has_no_streaming_filter_wiring_at_all():
    """Confirms the route (unlike for-you/movies-of-the-day) never touches
    streaming prefs — Most Popular This Week is deliberately never filtered."""
    source = inspect.getsource(movies_controller.trending)
    assert "streaming" not in source
