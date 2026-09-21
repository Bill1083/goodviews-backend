"""The cache helpers must be silent no-ops without Redis — every write path
in the app calls invalidate_user_stats, and none of them may fail because
the cache is down."""
from app.services import cache


def test_helpers_are_noops_without_redis(monkeypatch):
    monkeypatch.setattr(cache, "get_redis", lambda: None)
    assert cache.cache_get("k") is None
    cache.cache_set("k", {"a": 1}, ttl=10)          # no exception
    cache.cache_delete("k", "k2")                   # no exception
    assert cache.cache_incr("counter") is None
    assert cache.stats_version("user") == 0
    cache.invalidate_user_stats("user")             # no exception


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.expiries: dict[str, int] = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value
        self.expiries[key] = ttl

    def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)

    def incr(self, key):
        self.store[key] = str(int(self.store.get(key) or 0) + 1)
        return int(self.store[key])

    def expire(self, key, ttl):
        self.expiries[key] = ttl


def test_version_bumps_on_invalidate(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(cache, "get_redis", lambda: fake)
    assert cache.stats_version("u") == 0
    cache.invalidate_user_stats("u")
    cache.invalidate_user_stats("u")
    assert cache.stats_version("u") == 2
    assert fake.expiries["stats:ver:u"] == cache.STATS_VERSION_TTL_SECONDS
    cache.cache_set("stats:dashboard:u:UTC:v2", {"ok": True}, ttl=5)
    assert cache.cache_get("stats:dashboard:u:UTC:v2") == {"ok": True}
    cache.cache_delete("stats:dashboard:u:UTC:v2")
    assert cache.cache_get("stats:dashboard:u:UTC:v2") is None
