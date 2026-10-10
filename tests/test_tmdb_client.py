"""The TMDB client's failure behaviour: retries, the wall-clock budget, the
stale-search fallback, and case-insensitive search caching. No network — the
HTTP session, the clock and the cache are all faked."""
from types import SimpleNamespace

import pytest
import requests

from app import create_app
from app.services import tmdb


@pytest.fixture
def app_ctx():
    app = create_app()
    app.config.update(TMDB_API_KEY="k", TMDB_BASE_URL="https://tmdb.test", SEARCH_CACHE_TTL_SECONDS=60)
    with app.app_context():
        yield app


class Clock:
    """A fake monotonic clock; sleeping advances it instead of waiting."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeSession:
    """Plays back a script of outcomes, one per .get(): an exception to raise
    or a dict to return as the JSON body. Each call costs `cost` seconds."""

    def __init__(self, script, clock, cost=0.2):
        self.script = list(script)
        self.clock = clock
        self.cost = cost
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        outcome = self.script.pop(0) if self.script else requests.ConnectionError("script exhausted")
        # A hung server raises once the read timeout runs out, so a call never
        # costs more than the timeout it was given.
        cost = min(self.cost, timeout[1]) if timeout and isinstance(outcome, requests.Timeout) else self.cost
        self.clock.now += cost
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: outcome)


def install(monkeypatch, script, cost=0.2):
    clock = Clock()
    session = FakeSession(script, clock, cost)
    monkeypatch.setattr(tmdb, "_session", lambda: session)
    monkeypatch.setattr(tmdb.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(tmdb.time, "sleep", clock.sleep)
    return session, clock


def http_error(status):
    return requests.HTTPError(response=SimpleNamespace(status_code=status))


def test_retries_connection_resets_then_succeeds(app_ctx, monkeypatch):
    session, _ = install(monkeypatch, [requests.ConnectionError("reset"), requests.ConnectionError("reset"), {"ok": 1}])
    assert tmdb._tmdb_get("/movie/1") == {"ok": 1}
    assert len(session.calls) == 3


def test_a_4xx_is_not_retried(app_ctx, monkeypatch):
    session, _ = install(monkeypatch, [http_error(404), {"never": "reached"}])
    with pytest.raises(requests.HTTPError):
        tmdb._tmdb_get("/movie/1")
    assert len(session.calls) == 1


# ─── Search ranking: mainstream-ness, not raw TMDB "popularity" ────────────

def movie(mid, title, popularity, vote_count, vote_average=7.0):
    return {"id": mid, "title": title, "popularity": popularity, "vote_count": vote_count, "vote_average": vote_average}


def test_mainstream_score_favours_vote_count_over_a_bare_popularity_spike():
    """The actual reported case: a low-vote title with a temporary
    popularity spike (an obscure "Kung Fu Soccer") must not outrank a
    franchise entry with tens of thousands of accumulated votes."""
    mainstream = movie(9502, "Kung Fu Panda", popularity=30.8, vote_count=13064)
    spike = movie(1491920, "Kung Fu Soccer", popularity=67.66, vote_count=44)
    assert tmdb._mainstream_score(mainstream) > tmdb._mainstream_score(spike)


def test_sort_by_mainstream_score_reorders_results_in_place():
    data = {
        "results": [
            movie(1, "Low vote, high popularity spike", popularity=100, vote_count=5),
            movie(2, "High vote classic", popularity=10, vote_count=20000),
        ]
    }
    out = tmdb._sort_by_mainstream_score(data)
    assert [m["id"] for m in out["results"]] == [2, 1]


def test_a_genuinely_trending_new_release_can_still_rank_highly():
    """A title with very few votes so far (just released) but a large
    current popularity spike should still be able to outrank old catalog
    titles with modest vote counts — the blend isn't vote_count-only."""
    new_release = movie(1, "Brand New Release", popularity=500, vote_count=50)
    old_catalog = movie(2, "Forgotten Catalog Title", popularity=1, vote_count=200)
    assert tmdb._mainstream_score(new_release) > tmdb._mainstream_score(old_catalog)


# ─── Fuzzy "did you mean" fallback ──────────────────────────────────────────

def test_fuzzy_search_finds_a_badly_garbled_title(app_ctx, monkeypatch):
    corpus = [
        movie(105, "Back to the Future", popularity=40, vote_count=10000),
        movie(165, "Back to the Future Part II", popularity=20, vote_count=5000),
        movie(9502, "Kung Fu Panda", popularity=30, vote_count=13000),
    ]
    monkeypatch.setattr(tmdb, "get_popular_titles_corpus", lambda: corpus)
    matches = tmdb.fuzzy_search_movies("Back Tk the Fu")
    assert matches
    assert matches[0]["title"] == "Back to the Future"


def test_fuzzy_search_returns_nothing_for_an_unrelated_query(app_ctx, monkeypatch):
    corpus = [movie(105, "Back to the Future", popularity=40, vote_count=10000)]
    monkeypatch.setattr(tmdb, "get_popular_titles_corpus", lambda: corpus)
    assert tmdb.fuzzy_search_movies("xyzzy plugh quux") == []


def test_fuzzy_search_degrades_to_empty_when_the_corpus_is_unavailable(app_ctx, monkeypatch):
    monkeypatch.setattr(tmdb, "get_popular_titles_corpus", lambda: [])
    assert tmdb.fuzzy_search_movies("anything") == []


def test_search_movies_falls_back_to_fuzzy_matches_on_zero_results(app_ctx, monkeypatch):
    monkeypatch.setattr(tmdb, "_cached_search", lambda *a, **k: {"page": 1, "results": [], "total_pages": 1, "total_results": 0})
    corpus = [movie(105, "Back to the Future", popularity=40, vote_count=10000)]
    monkeypatch.setattr(tmdb, "get_popular_titles_corpus", lambda: corpus)

    data = tmdb.search_movies("Back Tk the Fu")

    assert data["fuzzy_fallback"] is True
    assert data["results"][0]["title"] == "Back to the Future"


def test_search_movies_does_not_fuzzy_fallback_when_results_already_exist(app_ctx, monkeypatch):
    real_result = {"page": 1, "results": [movie(1, "A Real Match", popularity=10, vote_count=100)], "total_pages": 1, "total_results": 1}
    monkeypatch.setattr(tmdb, "_cached_search", lambda *a, **k: real_result)
    called = []
    monkeypatch.setattr(tmdb, "fuzzy_search_movies", lambda q: called.append(q) or [])

    data = tmdb.search_movies("a real match")

    assert "fuzzy_fallback" not in data
    assert not called


def test_search_movies_does_not_fuzzy_fallback_on_pages_after_the_first(app_ctx, monkeypatch):
    monkeypatch.setattr(tmdb, "_cached_search", lambda *a, **k: {"page": 2, "results": [], "total_pages": 1, "total_results": 0})
    called = []
    monkeypatch.setattr(tmdb, "fuzzy_search_movies", lambda q: called.append(q) or [])

    data = tmdb.search_movies("whatever", page=2)

    assert not called
    assert data["results"] == []


def test_rate_limiting_is_retried(app_ctx, monkeypatch):
    session, _ = install(monkeypatch, [http_error(429), {"ok": 1}])
    assert tmdb._tmdb_get("/movie/1") == {"ok": 1}
    assert len(session.calls) == 2


def test_a_hanging_tmdb_gives_up_inside_the_budget(app_ctx, monkeypatch):
    """Every attempt times out at its full read timeout. Before the budget,
    six of these plus backoff held a worker for over a minute."""
    session, clock = install(monkeypatch, [requests.Timeout("slow")] * 6, cost=tmdb._TIMEOUT[1])
    start = clock.now
    with pytest.raises(requests.Timeout):
        tmdb._tmdb_get("/search/movie")
    assert clock.now - start <= tmdb._DEADLINE_SECONDS + 0.01
    assert len(session.calls) < 6


def test_each_attempt_is_capped_to_the_time_left(app_ctx, monkeypatch):
    session, _ = install(monkeypatch, [requests.Timeout("slow"), requests.Timeout("slow"), {"ok": 1}], cost=4.0)
    tmdb._tmdb_get("/movie/1")
    connect, read = session.calls[-1]["timeout"]
    assert read <= tmdb._DEADLINE_SECONDS - 8.0 + 0.01
    assert connect <= read


# ─── Search caching ──────────────────────────────────────────────────────────

class FakeCache:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ttl=None):
        self.store[key] = value


@pytest.fixture
def cache(monkeypatch):
    fake = FakeCache()
    monkeypatch.setattr(tmdb, "_cache_get", fake.get)
    monkeypatch.setattr(tmdb, "_cache_set", fake.set)
    return fake


def test_search_cache_ignores_case_and_spacing(app_ctx, monkeypatch, cache):
    session, _ = install(monkeypatch, [{"results": [{"id": 1, "popularity": 5}]}])
    tmdb.search_movies("Mirror Mask")
    tmdb.search_movies("  mirror   MASK ")
    assert len(session.calls) == 1


def test_search_falls_back_to_the_last_good_results_when_tmdb_is_down(app_ctx, monkeypatch, cache):
    install(monkeypatch, [{"results": [{"id": 1, "popularity": 5}]}])
    tmdb.search_movies("Coraline")

    # The fresh copy expires; TMDB is now unreachable.
    del cache.store[tmdb._search_key("search", "Coraline", 1)]
    install(monkeypatch, [requests.ConnectionError("down")] * 6)
    assert tmdb.search_movies("Coraline")["results"][0]["id"] == 1


def test_search_with_no_stale_copy_still_reports_the_failure(app_ctx, monkeypatch, cache):
    install(monkeypatch, [requests.ConnectionError("down")] * 6)
    with pytest.raises(requests.ConnectionError):
        tmdb.search_movies("never searched before")


def test_people_search_has_the_same_fallback(app_ctx, monkeypatch, cache):
    install(monkeypatch, [{"results": [{"id": 9, "popularity": 1}]}])
    tmdb.search_people("Tim Burton")
    del cache.store[tmdb._search_key("people:search", "Tim Burton", 1)]
    install(monkeypatch, [requests.ConnectionError("down")] * 6)
    assert tmdb.search_people("tim burton")["results"][0]["id"] == 9
