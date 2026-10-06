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
