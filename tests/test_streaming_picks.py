"""Curating TMDB's AU provider catalog down to recognizable names, and
filtering a feed to only films available (flatrate) on a given provider set."""
import pytest

from app import create_app
from app.services import movie_cache, streaming_picks, tmdb


def movie(id_, **over):
    base = {"id": id_, "title": f"Movie {id_}", "poster_path": None}
    base.update(over)
    return base


def providers_payload(region_flatrate: dict[int, list[int]] | None = None):
    """A movie_cache.get_movie()-shaped response with watch/providers for AU,
    where region_flatrate maps provider_id -> itself (just need the ids)."""
    flatrate = [{"provider_id": pid} for pid in (region_flatrate or [])]
    return {"watch/providers": {"results": {"AU": {"flatrate": flatrate}}}}


@pytest.fixture
def app_ctx():
    with create_app().app_context():
        yield


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


def test_orders_by_curated_rank_not_tmdb_order(monkeypatch, app_ctx):
    monkeypatch.setattr(tmdb, "get_watch_providers_list", lambda region: {"results": [
        {"provider_id": 337, "provider_name": "Disney Plus", "logo_path": "/d.png"},
        {"provider_id": 8, "provider_name": "Netflix", "logo_path": "/n.png"},
    ]})
    result = streaming_picks.list_streaming_providers()
    assert [p["provider_id"] for p in result] == [8, 337]


def test_dedupes_repeated_provider_ids(monkeypatch, app_ctx):
    monkeypatch.setattr(tmdb, "get_watch_providers_list", lambda region: {"results": [
        {"provider_id": 8, "provider_name": "Netflix", "logo_path": "/n.png"},
        {"provider_id": 8, "provider_name": "Netflix Standard with Ads", "logo_path": "/n2.png"},
    ]})
    assert len(streaming_picks.list_streaming_providers()) == 1


# ─── filter_by_availability ──────────────────────────────────────────────────

def test_empty_provider_ids_is_a_no_op(app_ctx):
    movies = [movie(1), movie(2)]
    assert streaming_picks.filter_by_availability(movies, []) == movies


def test_empty_movie_list_is_a_no_op(app_ctx):
    assert streaming_picks.filter_by_availability([], [8]) == []


def test_keeps_only_movies_available_on_a_selected_provider(monkeypatch, app_ctx):
    data = {1: providers_payload({8: None}), 2: providers_payload({337: None}), 3: providers_payload({})}
    monkeypatch.setattr(movie_cache, "get_movie", lambda movie_id, segments=(): data[movie_id])
    result = streaming_picks.filter_by_availability([movie(1), movie(2), movie(3)], [8])
    assert [m["id"] for m in result] == [1]


def test_a_movie_on_any_selected_provider_is_kept(monkeypatch, app_ctx):
    monkeypatch.setattr(movie_cache, "get_movie", lambda movie_id, segments=(): providers_payload({337: None}))
    result = streaming_picks.filter_by_availability([movie(1)], [8, 337])
    assert [m["id"] for m in result] == [1]


def test_preserves_input_order(monkeypatch, app_ctx):
    monkeypatch.setattr(movie_cache, "get_movie", lambda movie_id, segments=(): providers_payload({8: None}))
    result = streaming_picks.filter_by_availability([movie(3), movie(1), movie(2)], [8])
    assert [m["id"] for m in result] == [3, 1, 2]


def test_a_provider_lookup_failure_fails_open_and_keeps_the_movie(monkeypatch, app_ctx):
    def boom(movie_id, segments=()):
        raise RuntimeError("TMDB unreachable")

    monkeypatch.setattr(movie_cache, "get_movie", boom)
    result = streaming_picks.filter_by_availability([movie(1)], [8])
    assert [m["id"] for m in result] == [1]


def test_no_flatrate_entry_for_the_region_excludes_the_movie(monkeypatch, app_ctx):
    monkeypatch.setattr(movie_cache, "get_movie", lambda movie_id, segments=(): {"watch/providers": {"results": {}}})
    result = streaming_picks.filter_by_availability([movie(1)], [8])
    assert result == []
