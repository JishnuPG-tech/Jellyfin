"""Unit tests for apex_stream.cache — bounded LRU + TTL chunk cache."""

import time

from apex_stream.cache import BoundedChunkCache


class TestCacheBasics:
    def test_disabled_when_max_bytes_zero(self):
        cache = BoundedChunkCache(max_bytes=0)
        assert not cache.enabled
        cache.put(("s", 1), b"x" * 100)
        assert cache.get(("s", 1)) is None

    def test_put_get_roundtrip(self):
        cache = BoundedChunkCache(max_bytes=1024, ttl_seconds=60)
        key = ("s", 5)
        data = b"z" * 500
        cache.put(key, data)
        assert cache.get(key) == data
        assert cache.hits == 1
        assert cache.misses == 0

    def test_miss(self):
        cache = BoundedChunkCache(max_bytes=1024, ttl_seconds=60)
        assert cache.get(("s", 1)) is None
        assert cache.misses == 1

    def test_lru_eviction(self):
        cache = BoundedChunkCache(max_bytes=100, ttl_seconds=60)
        cache.put(("s", 1), b"a" * 40)
        cache.put(("s", 2), b"b" * 40)
        cache.put(("s", 3), b"c" * 40)  # 120 > 100 -> evict LRU
        assert cache.get(("s", 1)) is None
        assert cache.get(("s", 2)) is not None
        assert cache.get(("s", 3)) is not None
        assert cache.evictions >= 1

    def test_lru_recency_keeps_recent(self):
        cache = BoundedChunkCache(max_bytes=100, ttl_seconds=60)
        cache.put(("s", 1), b"a" * 40)
        cache.put(("s", 2), b"b" * 40)
        cache.get(("s", 1))  # refresh recency
        cache.put(("s", 3), b"c" * 40)
        # 1 and 2 are LRU; 1 was refreshed -> evict 2 first
        assert cache.get(("s", 1)) is not None
        assert cache.get(("s", 2)) is None
        assert cache.get(("s", 3)) is not None

    def test_overwrite_same_key(self):
        cache = BoundedChunkCache(max_bytes=1000, ttl_seconds=60)
        cache.put(("s", 1), b"a" * 100)
        cache.put(("s", 1), b"b" * 100)
        assert cache.get(("s", 1)) == b"b" * 100
        assert cache.bytes_used == 100

    def test_single_entry_larger_than_cache_is_dropped(self):
        cache = BoundedChunkCache(max_bytes=100, ttl_seconds=60)
        cache.put(("s", 1), b"a" * 500)
        assert cache.get(("s", 1)) is None


class TestCacheTTL:
    def test_ttl_expiry(self, monkeypatch):
        now = [time.monotonic()]
        monkeypatch.setattr(time, "monotonic", lambda: now[0])
        cache = BoundedChunkCache(max_bytes=1000, ttl_seconds=10)
        cache.put(("s", 1), b"x" * 100)
        assert cache.get(("s", 1)) is not None
        now[0] += 11
        assert cache.get(("s", 1)) is None
        assert cache.evictions >= 1

    def test_ttl_refresh_on_access(self, monkeypatch):
        now = [time.monotonic()]
        monkeypatch.setattr(time, "monotonic", lambda: now[0])
        cache = BoundedChunkCache(max_bytes=1000, ttl_seconds=10)
        cache.put(("s", 1), b"x" * 100)
        now[0] += 9
        cache.get(("s", 1))  # refresh
        now[0] += 9
        assert cache.get(("s", 1)) is not None


class TestCacheStats:
    def test_stats_and_hit_rate(self):
        cache = BoundedChunkCache(max_bytes=1024, ttl_seconds=60)
        cache.put(("s", 1), b"x" * 100)
        cache.get(("s", 1))
        cache.get(("s", 9))  # miss
        stats = cache.stats()
        assert stats["inserts"] == 1
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert cache.hit_rate() == 0.5

    def test_clear(self):
        cache = BoundedChunkCache(max_bytes=1024, ttl_seconds=60)
        cache.put(("s", 1), b"x" * 100)
        cache.clear()
        assert len(cache) == 0
        assert cache.bytes_used == 0
        assert cache.get(("s", 1)) is None