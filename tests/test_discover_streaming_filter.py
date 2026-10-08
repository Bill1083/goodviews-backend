"""GET /api/movies/for-you and /movies-of-the-day apply the "only show what
I can stream" filter when the user has it on in Settings; Most Popular This
Week never does — it isn't even user-aware (no @require_auth), so there's
no per-user preference to apply there in the first place.

The underlying recommendation/daily-picks engines and the actual TMDB
provider lookup are exercised by their own tests — this file is purely
about the wiring: does the route read the right prefs and apply the filter.
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


def movie(id_):
    return {"id": id_, "title": f"Movie {id_}"}


@pytest.fixture
def make_client(monkeypatch):
    def _make(profile_row: dict, missing: set[str] = frozenset(), for_you_results=None, daily_results=None):
        app = create_app()
        app.config.update(TESTING=True, RATELIMIT_ENABLED=False)

        fake_user = SimpleNamespace(id=ME, factors=[])
        monkeypatch.setattr(auth_utils, "_validate_token", lambda token: fake_user if token == "good" else None)

        table = FakeProfileTable(profile_row, missing)
        monkeypatch.setattr(movies_controller, "get_supabase", lambda: SimpleNamespace(table=lambda _name: table))

        monkeypatch.setattr(
            movies_controller.recommendations, "get_recommendations_for_user",
            lambda user_id, force=False: {
                "page": 1, "total_pages": 1,
                "total_results": len(for_you_results or []), "results": list(for_you_results or []),
            },
        )
        monkeypatch.setattr(
            movies_controller.daily_picks, "get_daily_picks",
            lambda user_id, tz, force=False: {
                "page": 1, "total_pages": 1,
                "total_results": len(daily_results or []), "results": list(daily_results or []),
            },
        )
        # Stand-in: "available" iff the movie's id is itself in provider_ids
        # — lets a test control availability just by choosing ids/providers,
        # without needing real TMDB-shaped provider payloads here too.
        monkeypatch.setattr(
            movies_controller.streaming_picks, "filter_by_availability",
            lambda movies, provider_ids: movies if not provider_ids else [m for m in movies if m["id"] in provider_ids],
        )

        return app.test_client()

    return _make


def test_for_you_is_unfiltered_when_the_toggle_is_off(make_client):
    row = {"id": ME, "streaming_filter_enabled": False, "streaming_provider_ids": [1]}
    client = make_client(row, for_you_results=[movie(1), movie(2)])
    body = client.get("/api/movies/for-you", headers=AUTH).get_json()
    assert [m["id"] for m in body["results"]] == [1, 2]
    assert body["total_results"] == 2


def test_for_you_is_filtered_when_the_toggle_is_on(make_client):
    row = {"id": ME, "streaming_filter_enabled": True, "streaming_provider_ids": [1]}
    client = make_client(row, for_you_results=[movie(1), movie(2)])
    body = client.get("/api/movies/for-you", headers=AUTH).get_json()
    assert [m["id"] for m in body["results"]] == [1]
    assert body["total_results"] == 1


def test_toggle_on_with_no_providers_selected_is_still_unfiltered(make_client):
    """Filtering "to my services" when none are picked would mean "show
    nothing" — treated the same as the toggle being off instead."""
    row = {"id": ME, "streaming_filter_enabled": True, "streaming_provider_ids": []}
    client = make_client(row, for_you_results=[movie(1), movie(2)])
    body = client.get("/api/movies/for-you", headers=AUTH).get_json()
    assert [m["id"] for m in body["results"]] == [1, 2]


def test_movies_of_the_day_is_filtered_the_same_way(make_client):
    row = {"id": ME, "streaming_filter_enabled": True, "streaming_provider_ids": [2]}
    client = make_client(row, daily_results=[movie(1), movie(2), movie(3)])
    body = client.get("/api/movies/movies-of-the-day?tz=UTC", headers=AUTH).get_json()
    assert [m["id"] for m in body["results"]] == [2]


def test_filter_prefs_missing_columns_read_as_off(make_client):
    """sql/012/013 not applied yet — no crash, just unfiltered."""
    row = {"id": ME, "streaming_filter_enabled": False, "streaming_provider_ids": []}
    client = make_client(row, missing={"streaming_filter_enabled", "streaming_provider_ids"},
                          for_you_results=[movie(1), movie(2)])
    body = client.get("/api/movies/for-you", headers=AUTH).get_json()
    assert [m["id"] for m in body["results"]] == [1, 2]


def test_trending_has_no_streaming_filter_wiring_at_all():
    """Confirms the route (unlike for-you/movies-of-the-day) never touches
    streaming_picks — Most Popular This Week is deliberately never filtered."""
    source = inspect.getsource(movies_controller.trending)
    assert "streaming_picks" not in source
