"""The "Your Streaming Services" pipeline: picking a curated provider list
out of TMDB's full catalog, and merging popular/well-rated/genre-affinity
discover pools into one carousel's worth of candidates."""
import pytest

from app import create_app
from app.services import streaming_picks, tmdb


def movie(id_, **over):
    base = {"id": id_, "title": f"Movie {id_}", "poster_path": None, "backdrop_path": None,
            "release_date": "2020-01-01", "overview": "", "vote_average": 7.0, "genre_ids": [28]}
    base.update(over)
    return base


class FakeCache:
    def __init__(self):
        self.store: dict = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ttl=None):
        self.store[key] = value


@pytest.fixture
def app_ctx():
    with create_app().app_context():
        yield


@pytest.fixture
def fake_cache(monkeypatch):
    cache = FakeCache()
    monkeypatch.setattr(streaming_picks, "cache_get", cache.get)
    monkeypatch.setattr(streaming_picks, "cache_set", cache.set)
    return cache


# ─── list_streaming_providers ────────────────────────────────────────────────

def test_keeps_only_curated_providers(monkeypatch, app_ctx):
    monkeypatch.setattr(tmdb, "get_watch_providers_list", lambda region: {"results": [
        {"provider_id": 8, "provider_name": "Netflix", "logo_path": "/n.png"},
        {"provider_id": 337, "provider_name": "Disney Plus", "logo_path": "/d.png"},
        {"provider_id": 999, "provider_name": "Some Obscure Rental Service", "logo_path": "/x.png"},
    ]})
    result = streaming_picks.list_streaming_providers()
    ids = [p["provider_id"] for p in result]
    assert 8 in ids and 337 in ids
    assert 999 not in ids


def test_orders_by_curated_keyword_rank_not_tmdb_order(monkeypatch, app_ctx):
    # Disney Plus listed first by TMDB, but netflix ranks earlier in
    # CURATED_PROVIDER_KEYWORDS — the curated order should win.
    monkeypatch.setattr(tmdb, "get_watch_providers_list", lambda region: {"results": [
        {"provider_id": 337, "provider_name": "Disney Plus", "logo_path": "/d.png"},
        {"provider_id": 8, "provider_name": "Netflix", "logo_path": "/n.png"},
    ]})
    result = streaming_picks.list_streaming_providers()
    assert [p["provider_id"] for p in result] == [8, 337]


def test_excludes_bundle_and_tier_variants_of_a_curated_name(monkeypatch, app_ctx):
    """A real AU catalog call turned these up: TMDB lists tier variants and
    channel-bundle listings as their own providers, which a substring match
    against "netflix"/"apple tv" would wrongly pull in alongside the real
    thing. Exact-name matching is what keeps them out."""
    monkeypatch.setattr(tmdb, "get_watch_providers_list", lambda region: {"results": [
        {"provider_id": 8, "provider_name": "Netflix", "logo_path": "/n.png"},
        {"provider_id": 175, "provider_name": "Netflix Kids", "logo_path": "/nk.png"},
        {"provider_id": 1796, "provider_name": "Netflix Standard with Ads", "logo_path": "/na.png"},
        {"provider_id": 350, "provider_name": "Apple TV", "logo_path": "/a.png"},
        {"provider_id": 2, "provider_name": "Apple TV Store", "logo_path": "/as.png"},
        {"provider_id": 1852, "provider_name": "Britbox Apple TV channel", "logo_path": "/bb.png"},
    ]})
    result = streaming_picks.list_streaming_providers()
    assert [p["provider_id"] for p in result] == [8, 350]


def test_dedupes_repeated_provider_ids(monkeypatch, app_ctx):
    monkeypatch.setattr(tmdb, "get_watch_providers_list", lambda region: {"results": [
        {"provider_id": 8, "provider_name": "Netflix", "logo_path": "/n.png"},
        {"provider_id": 8, "provider_name": "Netflix Standard with Ads", "logo_path": "/n2.png"},
    ]})
    result = streaming_picks.list_streaming_providers()
    assert len(result) == 1


# ─── get_streaming_picks ─────────────────────────────────────────────────────

def test_no_providers_selected_returns_nothing(app_ctx):
    assert streaming_picks.get_streaming_picks([], [], set()) == []


def test_merges_and_dedupes_across_pools(monkeypatch, app_ctx, fake_cache):
    def fake_discover(params):
        if params.get("with_genres"):
            return {"results": [movie(1), movie(2)]}
        if params.get("sort_by") == "vote_average.desc":
            return {"results": [movie(2), movie(3)]}  # 2 overlaps the genre pool
        return {"results": [movie(4), movie(5)]}  # popularity pool

    monkeypatch.setattr(tmdb, "discover_movies", fake_discover)
    results = streaming_picks.get_streaming_picks([8], [28], set())
    ids = [m["id"] for m in results]
    assert len(ids) == len(set(ids))  # no duplicate despite movie 2 appearing twice
    assert {1, 2, 3, 4, 5} <= set(ids)


def test_excludes_already_seen_movie_ids(monkeypatch, app_ctx, fake_cache):
    monkeypatch.setattr(tmdb, "discover_movies", lambda params: {"results": [movie(1), movie(2), movie(3)]})
    results = streaming_picks.get_streaming_picks([8], [], {2})
    ids = {m["id"] for m in results}
    assert 2 not in ids
    assert {1, 3} <= ids


def test_discover_pool_is_cached_across_calls_with_the_same_providers(monkeypatch, app_ctx, fake_cache):
    calls = []

    def fake_discover(params):
        calls.append(params)
        return {"results": [movie(1)]}

    monkeypatch.setattr(tmdb, "discover_movies", fake_discover)
    streaming_picks.get_streaming_picks([8], [], set())
    first_call_count = len(calls)
    streaming_picks.get_streaming_picks([8], [], set())
    # Same provider set, same pools -> second call should hit cache for at
    # least the top_rated pool (no randomness there), not re-fetch everything.
    assert len(calls) < first_call_count * 2
