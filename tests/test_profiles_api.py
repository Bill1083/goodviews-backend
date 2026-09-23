"""Public profiles and the privacy rules around them.

profile_visibility decides whether a profile opens at all, hide_friends_list
decides whether its friends come with it, and neither the profile nor the
stats route may leak the private half of the row. PostgREST is faked so the
rules are exercised without a database.
"""
from types import SimpleNamespace

import pytest

from app import create_app
from app.controllers import profile as profile_controller
from app.controllers import stats as stats_controller
from app.services.stats import TasteData
from app.utils import auth as auth_utils
from app.utils import social as social_utils

ME = "me-id"


def profile_row(pid: str, username: str, visibility: str = "friends_only", hide_friends: bool = False, **over) -> dict:
    row = {
        "id": pid,
        "username": username,
        "bio": f"{username}'s bio",
        "avatar_url": None,
        "avatar_color": "#c5c491",
        "avatar_focal_y": 22,
        "avatar_zoom": 1,
        "profile_visibility": visibility,
        "hide_friends_list": hide_friends,
        # The private half — must never reach a visitor.
        "hide_recent_movies": False,
        "mute_recommendations": True,
        "mute_friend_requests": True,
        "has_onboarded": True,
        "onboarding_genre_ids": [28],
    }
    row.update(over)
    return row


PROFILES = [
    profile_row(ME, "me"),
    profile_row("friend-id", "mate"),
    profile_row("stranger-id", "stranger"),
    profile_row("open-id", "openbook", visibility="everyone"),
    profile_row("closed-id", "hermit", visibility="no_one"),
    profile_row("shy-id", "shy", visibility="everyone", hide_friends=True),
    profile_row("legacy-id", "legacy", visibility=None),
]
BY_ID = {p["id"]: p for p in PROFILES}


def friendship(a: str, b: str) -> dict:
    other = BY_ID[b]
    return {
        "user_id": a,
        "friend_id": b,
        "id": f"{a}->{b}",
        "created_at": "2026-01-01T00:00:00+00:00",
        "profiles": {k: other[k] for k in ("id", "username", "avatar_url", "avatar_color", "avatar_focal_y", "avatar_zoom")},
    }


def both_ways(a: str, b: str) -> list[dict]:
    return [friendship(a, b), friendship(b, a)]


FRIENDSHIPS = [
    *both_ways(ME, "friend-id"),
    *both_ways(ME, "closed-id"),
    *both_ways("friend-id", "open-id"),
    *both_ways("shy-id", "open-id"),
    *both_ways("legacy-id", "stranger-id"),
]


class FakeTable:
    """Just enough PostgREST for these routes: select/eq/limit/order/single."""

    def __init__(self, rows: list[dict]):
        self._rows = list(rows)
        self._cols: str | None = None
        self._single = False

    def select(self, cols: str, **_kw):
        self._cols = cols
        return self

    def eq(self, col: str, val):
        self._rows = [r for r in self._rows if str(r.get(col)) == str(val)]
        return self

    def limit(self, n: int):
        self._rows = self._rows[:n]
        return self

    def order(self, *_a, **_k):
        return self

    def single(self):
        self._single = True
        return self

    def execute(self):
        rows = self._rows
        # Projection matters here: a route that returned the raw row instead of
        # building its own dict would start leaking the private columns.
        if self._cols and "(" not in self._cols and "*" not in self._cols:
            names = [c.strip() for c in self._cols.split(",")]
            rows = [{k: r.get(k) for k in names} for r in rows]
        if self._single:
            if not rows:
                raise RuntimeError("no rows for single()")
            return SimpleNamespace(data=rows[0])
        return SimpleNamespace(data=rows)


class FakeSupabase:
    def __init__(self, tables: dict[str, list[dict]]):
        self._tables = tables

    def table(self, name: str):
        return FakeTable(self._tables.get(name, []))


@pytest.fixture
def client(monkeypatch):
    app = create_app()
    app.config.update(TESTING=True, RATELIMIT_ENABLED=False)

    fake_user = SimpleNamespace(id=ME, factors=[])
    monkeypatch.setattr(auth_utils, "_validate_token", lambda token: fake_user if token == "good" else None)

    supabase = FakeSupabase({"profiles": PROFILES, "friendships": FRIENDSHIPS})
    monkeypatch.setattr(profile_controller, "get_supabase", lambda: supabase)
    monkeypatch.setattr(stats_controller, "get_supabase", lambda: supabase)
    monkeypatch.setattr(social_utils, "get_supabase", lambda: supabase, raising=False)

    # Stats: the gate is what's under test, so the aggregation gets a fixed input.
    def fake_load(user_id, sb, **kwargs):
        return TasteData(user_id=user_id, reviews=[{
            "id": "r1", "movie_id": 1, "rating": 5, "review_text": "", "rewatch_count": 0,
            "category_ids": [], "is_onboarding": False, "created_at": "2026-05-01T10:00:00+00:00",
            "movies": {"id": 1, "title": "Film", "poster_path": None, "backdrop_path": None,
                       "release_date": "2011-01-01", "runtime": 100, "genre_ids": [35],
                       "vote_average": 7.0, "vote_count": 300, "popularity": 10.0,
                       "original_language": "en", "production_countries": ["US"], "budget": None,
                       "revenue": None, "collection_id": None, "collection_name": None,
                       "tagline": None, "directors": [], "top_cast": []},
        }])

    monkeypatch.setattr(stats_controller.stats, "load_taste_data", fake_load)
    monkeypatch.setattr(stats_controller, "cache_get", lambda key: None)
    monkeypatch.setattr(stats_controller, "cache_set", lambda key, value, ttl=None: None)
    monkeypatch.setattr(stats_controller, "stats_version", lambda user_id: 0)

    with app.test_client() as c:
        yield c


AUTH = {"Authorization": "Bearer good"}


def get_profile(client, user_id: str):
    return client.get(f"/api/profile/{user_id}", headers=AUTH)


def test_requires_auth(client):
    assert client.get("/api/profile/open-id").status_code == 401
    assert client.get("/api/stats/user/open-id").status_code == 401


# ─── Who may open a profile ──────────────────────────────────────────────────

def test_friends_can_view_a_friends_only_profile(client):
    res = get_profile(client, "friend-id")
    assert res.status_code == 200
    assert res.get_json()["username"] == "mate"
    assert res.get_json()["is_friend"] is True


def test_a_stranger_cannot_view_a_friends_only_profile(client):
    res = get_profile(client, "stranger-id")
    assert res.status_code == 403
    assert res.get_json() == {"error": "private"}


def test_anyone_can_view_an_everyone_profile(client):
    res = get_profile(client, "open-id")
    assert res.status_code == 200
    assert res.get_json()["is_friend"] is False


def test_no_one_means_no_one_even_for_friends(client):
    assert get_profile(client, "closed-id").status_code == 403


def test_missing_visibility_is_treated_as_friends_only(client):
    """A null column must not read as "public" — it defaults to friends only."""
    assert get_profile(client, "legacy-id").status_code == 403


def test_your_own_profile_is_always_viewable(client):
    res = get_profile(client, ME)
    assert res.status_code == 200
    body = res.get_json()
    assert body["is_self"] is True and body["is_friend"] is False


def test_unknown_profile_is_404(client):
    assert get_profile(client, "nobody").status_code == 404


# ─── What comes back ─────────────────────────────────────────────────────────

def test_the_private_half_of_the_row_never_ships(client):
    body = get_profile(client, "open-id").get_json()
    assert set(body) == {
        "id", "username", "bio", "avatar_url", "avatar_color", "avatar_focal_y",
        "avatar_zoom", "is_self", "is_friend", "friends", "friend_count",
    }


def test_friends_are_listed_when_not_hidden(client):
    body = get_profile(client, "open-id").get_json()
    assert body["friend_count"] == 2
    assert sorted(f["username"] for f in body["friends"]) == ["mate", "shy"]


def test_hiding_the_friends_list_hides_the_count_too(client):
    body = get_profile(client, "shy-id").get_json()
    assert body["friends"] is None
    assert body["friend_count"] is None
    assert body["username"] == "shy"  # the rest of the profile still shows


# ─── Their stats follow the same gate ────────────────────────────────────────

def test_stats_are_readable_for_a_viewable_profile(client):
    res = client.get("/api/stats/user/open-id?tz=Australia/Sydney", headers=AUTH)
    assert res.status_code == 200
    body = res.get_json()
    assert body["headline"]["films"] == 1
    assert set(body) == {"generated_at", "tz", "coverage", "headline", "genres", "genre_highlights", "watchlist"}


def test_stats_are_refused_for_a_private_profile(client):
    assert client.get("/api/stats/user/stranger-id", headers=AUTH).status_code == 403
    assert client.get("/api/stats/user/closed-id", headers=AUTH).status_code == 403
    assert client.get("/api/stats/user/nobody", headers=AUTH).status_code == 404


def test_stats_validate_the_timezone_like_the_self_route(client):
    assert client.get("/api/stats/user/open-id?tz=Mars/Olympus", headers=AUTH).status_code == 400


# ─── The helper, directly ────────────────────────────────────────────────────

def test_are_friends_is_symmetric_and_self_is_always_true():
    supabase = FakeSupabase({"profiles": PROFILES, "friendships": FRIENDSHIPS})
    assert social_utils.are_friends(supabase, ME, "friend-id") is True
    assert social_utils.are_friends(supabase, "friend-id", ME) is True
    assert social_utils.are_friends(supabase, ME, "stranger-id") is False
    assert social_utils.are_friends(supabase, ME, ME) is True
