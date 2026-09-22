"""HTTP-level tests for /api/stats: auth, timezone validation, the Wrapped
unlock window (nothing announced before December, 423 on the year itself) and
the year-round history. Supabase and the aggregation loaders are stubbed —
the maths is covered in test_stats.py."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app import create_app
from app.controllers import stats as stats_controller
from app.services.stats import TasteData
from app.utils import auth as auth_utils

UTC = timezone.utc
SEPTEMBER = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
DECEMBER = datetime(2026, 12, 3, 12, 0, tzinfo=UTC)
NEW_YEAR = datetime(2027, 1, 2, 12, 0, tzinfo=UTC)


def _movie(mid: int) -> dict:
    return {
        "id": mid, "title": f"Film {mid}", "poster_path": None, "backdrop_path": None,
        "release_date": "2011-01-01", "runtime": 100, "genre_ids": [35], "vote_average": 6.5,
        "vote_count": 300, "popularity": 20.0, "original_language": "en", "production_countries": ["US"],
        "budget": None, "revenue": None, "collection_id": None, "collection_name": None,
        "tagline": None, "directors": [], "top_cast": [],
    }


def _review(rid: int, created_at: str, rating: int = 4) -> dict:
    return {
        "id": f"r{rid}", "movie_id": rid, "rating": rating, "review_text": "", "rewatch_count": 0,
        "category_ids": [], "is_onboarding": False, "created_at": created_at, "movies": _movie(rid),
    }


def _reviews() -> list[dict]:
    rows = [_review(i, f"2026-0{1 + i % 6}-10T20:00:00+00:00") for i in range(1, 8)]        # 7 in 2026
    rows += [_review(i, "2025-06-10T20:00:00+00:00") for i in range(8, 14)]                 # 6 in 2025
    rows += [_review(i, "2024-06-10T20:00:00+00:00") for i in range(14, 16)]                # 2 in 2024
    return rows


@pytest.fixture
def client(monkeypatch):
    app = create_app()
    app.config.update(TESTING=True, RATELIMIT_ENABLED=False, WRAPPED_PREVIEW_UNLOCK=False, WRAPPED_MIN_FILMS=5)

    fake_user = SimpleNamespace(id="user-1", factors=[])
    monkeypatch.setattr(auth_utils, "_validate_token", lambda token: fake_user if token == "good" else None)
    monkeypatch.setattr(stats_controller, "get_supabase", lambda: object())
    monkeypatch.setattr(
        stats_controller.stats, "load_taste_data",
        lambda user_id, sb, **kwargs: TasteData(user_id=user_id, reviews=_reviews()),
    )
    monkeypatch.setattr(
        stats_controller.stats, "load_review_dates",
        lambda user_id, sb: [datetime.fromisoformat(r["created_at"]) for r in _reviews()],
    )
    # No Redis in tests: helpers are no-ops (see test_cache.py).
    monkeypatch.setattr(stats_controller, "cache_get", lambda key: None)
    monkeypatch.setattr(stats_controller, "cache_set", lambda key, value, ttl=None: None)
    monkeypatch.setattr(stats_controller, "stats_version", lambda user_id: 0)
    monkeypatch.setattr(stats_controller, "_now", lambda: SEPTEMBER)

    with app.test_client() as c:
        c.app = app
        yield c


AUTH = {"Authorization": "Bearer good"}


def test_requires_auth(client):
    assert client.get("/api/stats/me").status_code == 401
    assert client.get("/api/stats/wrapped/2025", headers={"Authorization": "Bearer bad"}).status_code == 401


def test_dashboard_returns_the_trimmed_contract_and_validates_tz(client):
    res = client.get("/api/stats/me?tz=Australia/Sydney", headers=AUTH)
    assert res.status_code == 200
    body = res.get_json()
    assert set(body) == {"generated_at", "tz", "coverage", "headline", "genres", "genre_highlights", "watchlist"}
    assert body["headline"]["films"] == 15
    assert body["tz"] == "Australia/Sydney"

    assert client.get("/api/stats/me?tz=Mars/Olympus", headers=AUTH).status_code == 400


def test_before_december_the_profile_is_told_nothing_about_this_year(client):
    body = client.get("/api/stats/wrapped", headers=AUTH).get_json()
    assert body["current_year"] == 2026
    assert body["current"] is None
    # Only past years that actually produced a Wrapped; 2024 had 2 films.
    assert body["history"] == [{"year": 2025, "films": 6}]


def test_current_year_is_423_until_december_then_ready(client, monkeypatch):
    res = client.get("/api/stats/wrapped/2026", headers=AUTH)
    assert res.status_code == 423
    assert res.get_json() == {"error": "locked", "year": 2026, "unlocks_at": "2026-12-01T00:00:00+00:00"}

    monkeypatch.setattr(stats_controller, "_now", lambda: DECEMBER)
    res = client.get("/api/stats/wrapped/2026", headers=AUTH)
    assert res.status_code == 200
    body = res.get_json()
    assert body["status"] == "ready" and body["films"] == 7
    assert [s["kind"] for s in body["slides"]][:2] == ["intro", "volume"]

    availability = client.get("/api/stats/wrapped", headers=AUTH).get_json()
    assert availability["current"] == {"year": 2026, "status": "ready", "films": 7, "min_films": 5}
    assert availability["history"] == [{"year": 2025, "films": 6}]


def test_in_the_new_year_last_year_moves_into_the_history(client, monkeypatch):
    monkeypatch.setattr(stats_controller, "_now", lambda: NEW_YEAR)
    body = client.get("/api/stats/wrapped", headers=AUTH).get_json()
    assert body["current_year"] == 2027
    assert body["current"] is None
    assert body["history"] == [{"year": 2026, "films": 7}, {"year": 2025, "films": 6}]
    assert client.get("/api/stats/wrapped/2026", headers=AUTH).status_code == 200


def test_preview_flag_unlocks_current_year_locally(client):
    client.app.config["WRAPPED_PREVIEW_UNLOCK"] = True
    assert client.get("/api/stats/wrapped/2026", headers=AUTH).status_code == 200
    assert client.get("/api/stats/wrapped", headers=AUTH).get_json()["current"]["year"] == 2026


def test_past_year_below_threshold_and_out_of_range_years(client):
    res = client.get("/api/stats/wrapped/2024", headers=AUTH)
    assert res.status_code == 200
    assert res.get_json()["status"] == "not_enough"
    assert client.get("/api/stats/wrapped/2023", headers=AUTH).status_code == 404  # no activity
    assert client.get("/api/stats/wrapped/2030", headers=AUTH).status_code == 404  # future
    assert client.get("/api/stats/wrapped/1999", headers=AUTH).status_code == 404  # before the app existed
