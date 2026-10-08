"""Turning the "only show what I can stream" filter on changes *which*
films show in For You / Movies of the Day, not how many — both feeds
backfill from the same reservoirs their own dismiss mechanics already use
(see recommendations.get_recommendations_for_user_streaming and
daily_picks.get_daily_picks_streaming)."""
import time

import pytest

from app import create_app
from app.services import daily_picks, recommendations, streaming_picks
from tests.fakes import FakeSupabase

AVAILABLE = {1, 3, 5, 7, 9, 11, 13}  # odd ids "available"; even ids filtered out


def available_only(movies, provider_ids):
    if not provider_ids:
        return movies
    return [m for m in movies if m["id"] in AVAILABLE]


@pytest.fixture
def app_ctx():
    with create_app().app_context():
        yield


@pytest.fixture(autouse=True)
def fake_hydrate(monkeypatch):
    # Same trick test_recommendations_dismiss.py uses — skip the real movies
    # table round-trip, {movie_id, reason} -> {id: movie_id} is enough here.
    monkeypatch.setattr(recommendations, "_hydrate", lambda items, sb: {
        "page": 1, "total_pages": 1, "total_results": len(items),
        "results": [{"id": it["movie_id"]} for it in items],
    })


def feed_row(items, overflow=()):
    return [{
        "user_id": "u1",
        "items": [{"movie_id": m, "reason": "r"} for m in items],
        "overflow": [{"movie_id": m, "reason": "r"} for m in overflow],
    }]


# ─── get_recommendations_for_user_streaming ──────────────────────────────────

def test_no_providers_selected_returns_the_base_feed_unfiltered(app_ctx, monkeypatch):
    base = {"page": 1, "total_pages": 1, "total_results": 3, "results": [{"id": 1}, {"id": 2}, {"id": 3}]}
    monkeypatch.setattr(recommendations, "get_recommendations_for_user", lambda user_id, force=False: base)
    result = recommendations.get_recommendations_for_user_streaming("u1", [], force=False)
    assert result is base


def test_nothing_filtered_out_is_a_no_op(app_ctx, monkeypatch):
    base = {"page": 1, "total_pages": 1, "total_results": 2, "results": [{"id": 1}, {"id": 3}]}
    monkeypatch.setattr(recommendations, "get_recommendations_for_user", lambda user_id, force=False: base)
    monkeypatch.setattr(streaming_picks, "filter_by_availability", available_only)
    result = recommendations.get_recommendations_for_user_streaming("u1", [8], force=False)
    assert [m["id"] for m in result["results"]] == [1, 3]


def test_backfills_from_overflow_to_restore_the_original_count(app_ctx, monkeypatch):
    base = {"page": 1, "total_pages": 1, "total_results": 3, "results": [{"id": 1}, {"id": 2}, {"id": 3}]}
    monkeypatch.setattr(recommendations, "get_recommendations_for_user", lambda user_id, force=False: base)
    monkeypatch.setattr(streaming_picks, "filter_by_availability", available_only)
    db = FakeSupabase({"user_recommendations": feed_row([1, 2, 3], overflow=[4, 5, 6])})  # 5 is the only available one in overflow
    monkeypatch.setattr(recommendations, "get_supabase", lambda: db)

    result = recommendations.get_recommendations_for_user_streaming("u1", [8], force=False)

    assert len(result["results"]) == 3
    ids = {m["id"] for m in result["results"]}
    assert {1, 3, 5} <= ids
    assert 2 not in ids  # the filtered-out one is gone, not just topped up alongside it


def test_never_exceeds_the_original_count(app_ctx, monkeypatch):
    base = {"page": 1, "total_pages": 1, "total_results": 2, "results": [{"id": 1}, {"id": 2}]}
    monkeypatch.setattr(recommendations, "get_recommendations_for_user", lambda user_id, force=False: base)
    monkeypatch.setattr(streaming_picks, "filter_by_availability", available_only)
    db = FakeSupabase({"user_recommendations": feed_row([1, 2], overflow=[3, 5, 7, 9, 11])})  # plenty of available replacements
    monkeypatch.setattr(recommendations, "get_supabase", lambda: db)

    result = recommendations.get_recommendations_for_user_streaming("u1", [8], force=False)
    assert len(result["results"]) == 2


def test_falls_back_to_generic_backfill_once_overflow_is_exhausted(app_ctx, monkeypatch):
    base = {"page": 1, "total_pages": 1, "total_results": 2, "results": [{"id": 1}, {"id": 2}]}
    monkeypatch.setattr(recommendations, "get_recommendations_for_user", lambda user_id, force=False: base)
    monkeypatch.setattr(streaming_picks, "filter_by_availability", available_only)
    db = FakeSupabase({"user_recommendations": feed_row([1, 2], overflow=[4, 6])})  # overflow has nothing available
    monkeypatch.setattr(recommendations, "get_supabase", lambda: db)
    monkeypatch.setattr(recommendations, "_backfill_items", lambda exclude_ids, limit, supabase, deadline=None: [{"movie_id": 99, "reason": "Popular right now"}])

    result = recommendations.get_recommendations_for_user_streaming("u1", [8], force=False)
    ids = {m["id"] for m in result["results"]}
    assert 1 in ids and 99 not in ids  # 99 is even -> still filtered out by available_only, proving the backfilled batch is itself re-filtered


def test_drained_overflow_is_persisted(app_ctx, monkeypatch):
    base = {"page": 1, "total_pages": 1, "total_results": 1, "results": [{"id": 2}]}
    monkeypatch.setattr(recommendations, "get_recommendations_for_user", lambda user_id, force=False: base)
    monkeypatch.setattr(streaming_picks, "filter_by_availability", available_only)
    db = FakeSupabase({"user_recommendations": feed_row([2], overflow=[4, 5])})
    monkeypatch.setattr(recommendations, "get_supabase", lambda: db)

    recommendations.get_recommendations_for_user_streaming("u1", [8], force=False)

    remaining_overflow = [it["movie_id"] for it in db.tables["user_recommendations"][0]["overflow"]]
    assert 4 not in remaining_overflow  # popped while looking for an available one
    assert 5 not in remaining_overflow  # popped — it's the one that was found and used


def test_a_hopeless_selection_terminates_rather_than_hanging(app_ctx, monkeypatch):
    """Every candidate is even (never available) — must still return, not loop forever."""
    base = {"page": 1, "total_pages": 1, "total_results": 2, "results": [{"id": 2}, {"id": 4}]}
    monkeypatch.setattr(recommendations, "get_recommendations_for_user", lambda user_id, force=False: base)
    monkeypatch.setattr(streaming_picks, "filter_by_availability", available_only)
    db = FakeSupabase({"user_recommendations": feed_row([2, 4], overflow=[6, 8, 10, 12])})
    monkeypatch.setattr(recommendations, "get_supabase", lambda: db)
    monkeypatch.setattr(recommendations, "_backfill_items", lambda exclude_ids, limit, supabase, deadline=None: [])

    result = recommendations.get_recommendations_for_user_streaming("u1", [8], force=False)
    assert result["results"] == []


# ─── get_daily_picks_streaming ────────────────────────────────────────────────

def test_daily_no_providers_selected_returns_unfiltered(app_ctx, monkeypatch):
    base = {"page": 1, "total_pages": 1, "total_results": 3, "results": [{"id": 1}, {"id": 2}, {"id": 3}]}
    monkeypatch.setattr(daily_picks, "get_daily_picks", lambda user_id, tz, force=False: base)
    result = daily_picks.get_daily_picks_streaming("u1", object(), [], force=False)
    assert result is base


def test_daily_drops_and_refetches_until_all_three_pass(app_ctx, monkeypatch):
    calls = []
    responses = [
        {"page": 1, "total_pages": 1, "total_results": 3, "results": [{"id": 1}, {"id": 2}, {"id": 3}]},
        {"page": 1, "total_pages": 1, "total_results": 3, "results": [{"id": 1}, {"id": 3}, {"id": 5}]},
    ]

    def fake_get(user_id, tz, force=False):
        return responses[min(len(calls), len(responses) - 1)]

    def fake_drop(supabase, user_id, movie_id):
        calls.append(movie_id)
        return True

    monkeypatch.setattr(daily_picks, "get_daily_picks", fake_get)
    monkeypatch.setattr(daily_picks, "drop_from_daily_picks", fake_drop)
    monkeypatch.setattr(daily_picks, "get_supabase", lambda: FakeSupabase({}))
    monkeypatch.setattr(streaming_picks, "filter_by_availability", available_only)

    result = daily_picks.get_daily_picks_streaming("u1", object(), [8], force=False)

    assert calls == [2]  # only the unavailable one was dropped
    assert [m["id"] for m in result["results"]] == [1, 3, 5]
    assert result["total_results"] == 3


def test_daily_a_hopeless_selection_terminates_rather_than_hanging(app_ctx, monkeypatch):
    stuck = {"page": 1, "total_pages": 1, "total_results": 2, "results": [{"id": 2}, {"id": 4}]}
    monkeypatch.setattr(daily_picks, "get_daily_picks", lambda user_id, tz, force=False: stuck)
    monkeypatch.setattr(daily_picks, "drop_from_daily_picks", lambda supabase, user_id, movie_id: True)
    monkeypatch.setattr(daily_picks, "get_supabase", lambda: FakeSupabase({}))
    monkeypatch.setattr(streaming_picks, "filter_by_availability", available_only)

    result = daily_picks.get_daily_picks_streaming("u1", object(), [8], force=False)
    assert result["results"] == []
    assert result["total_results"] == 0


# ─── _backfill_items' own deadline ────────────────────────────────────────────

def test_backfill_items_stops_calling_tmdb_once_past_its_deadline(app_ctx, monkeypatch):
    """The gap that let a single streaming-filtered request run far longer
    than intended even with filter_by_availability itself bounded:
    _backfill_items could still attempt up to 3 TMDB top-rated pages with
    nothing stopping it, each able to take ~10s on its own (TMDB's own
    retry deadline) if the connection-reset flakiness documented in
    tmdb._tmdb_get was in play. A deadline already in the past means it
    shouldn't even attempt page 1."""
    calls = []
    monkeypatch.setattr(recommendations.tmdb, "get_top_rated_movies", lambda page=1: (calls.append(page), {"results": [movie(100 + page)]})[1])

    result = recommendations._backfill_items(set(), 10, object(), deadline=time.monotonic() - 1)

    assert calls == []
    assert result == []
