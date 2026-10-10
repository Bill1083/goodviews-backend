"""build_taste_profile_terms / overview_similarity — the content-based
signal that lets two users with an identical genre/person affinity profile
still get differently-ranked results, built entirely from movies they've
already rated (never anything they typed in)."""
from types import SimpleNamespace

import pytest

from app import create_app
from app.services import recommendations


@pytest.fixture
def app_ctx():
    with create_app().app_context():
        yield


def signals_with(positive_seeds=(), diversity_seeds=(), seed_info=None):
    return SimpleNamespace(
        positive_seeds=list(positive_seeds),
        diversity_seeds=list(diversity_seeds),
        seed_info=seed_info or {},
    )


# ─── build_taste_profile_terms ──────────────────────────────────────────────

def test_no_seeds_at_all_returns_none():
    assert recommendations.build_taste_profile_terms(signals_with()) is None


def test_seeds_present_but_none_have_cached_overview_returns_none():
    signals = signals_with(
        positive_seeds=[{"movie_id": 1, "rating": 5, "rewatch_count": 0}],
        seed_info={1: {"genre_ids": [28], "overview": None}},
    )
    assert recommendations.build_taste_profile_terms(signals) is None


def test_mixed_seeds_only_the_ones_with_overview_contribute():
    signals = signals_with(
        positive_seeds=[
            {"movie_id": 1, "rating": 5, "rewatch_count": 0},
            {"movie_id": 2, "rating": 4, "rewatch_count": 0},
        ],
        seed_info={
            1: {"overview": "A detective hunts a serial killer in a rainy city."},
            2: {"overview": None},
        },
    )
    terms = recommendations.build_taste_profile_terms(signals)
    assert terms is not None
    assert terms["detective"] > 0
    assert terms["killer"] > 0


def test_a_movie_not_in_positive_or_diversity_seeds_is_ignored():
    """seed_info can hold entries for movies outside positive/diversity
    seeds too — e.g. a negative (1-2*) seed, since _load_user_signals
    batches credits for everything in one call. build_taste_profile_terms
    must only ever read positive_seeds/diversity_seeds, never leak a
    disliked movie's overview text into the profile."""
    signals = signals_with(
        positive_seeds=[],
        diversity_seeds=[],
        seed_info={1: {"overview": "a movie rated one star and hated"}},
    )
    assert recommendations.build_taste_profile_terms(signals) is None


def test_a_higher_rated_seed_contributes_more_weight():
    five_star = signals_with(
        positive_seeds=[{"movie_id": 1, "rating": 5, "rewatch_count": 0}],
        seed_info={1: {"overview": "space pirates battle robots"}},
    )
    three_star = signals_with(
        diversity_seeds=[{"movie_id": 1, "rating": 3, "rewatch_count": 0}],
        seed_info={1: {"overview": "space pirates battle robots"}},
    )
    terms_5 = recommendations.build_taste_profile_terms(five_star)
    terms_3 = recommendations.build_taste_profile_terms(three_star)
    assert terms_5["pirates"] > terms_3["pirates"]


# ─── overview_similarity ─────────────────────────────────────────────────────

def test_identical_text_scores_close_to_one():
    profile = recommendations.build_taste_profile_terms(signals_with(
        positive_seeds=[{"movie_id": 1, "rating": 5, "rewatch_count": 0}],
        seed_info={1: {"overview": "a lonely robot explores an abandoned space station"}},
    ))
    score = recommendations.overview_similarity(profile, "a lonely robot explores an abandoned space station")
    assert score == pytest.approx(1.0, abs=1e-6)


def test_disjoint_vocabularies_score_zero():
    profile = recommendations.build_taste_profile_terms(signals_with(
        positive_seeds=[{"movie_id": 1, "rating": 5, "rewatch_count": 0}],
        seed_info={1: {"overview": "space pirates battle robots"}},
    ))
    assert recommendations.overview_similarity(profile, "a quiet romance blossoms over dinner") == 0.0


def test_missing_candidate_overview_scores_zero_without_raising():
    profile = recommendations.build_taste_profile_terms(signals_with(
        positive_seeds=[{"movie_id": 1, "rating": 5, "rewatch_count": 0}],
        seed_info={1: {"overview": "space pirates battle robots"}},
    ))
    assert recommendations.overview_similarity(profile, None) == 0.0
    assert recommendations.overview_similarity(profile, "") == 0.0


def test_no_profile_scores_zero_without_raising():
    assert recommendations.overview_similarity(None, "space pirates battle robots") == 0.0


# ─── _fetch_credits carries overview through (regression guard) ────────────

def test_fetch_credits_includes_overview_from_the_cache_response(app_ctx, monkeypatch):
    monkeypatch.setattr(
        recommendations.movie_cache,
        "get_movie",
        lambda mid, segments=None: {
            "title": "Film", "poster_path": "/p.jpg", "release_date": "2020-01-01",
            "genre_ids": [28], "vote_average": 7.0, "overview": "a detective hunts a killer",
            "credits": {"cast": [], "crew": []},
        },
    )
    info = recommendations._fetch_credits([1])
    assert info[1]["overview"] == "a detective hunts a killer"
