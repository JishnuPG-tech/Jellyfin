"""Bounded LRU + TTL hot-chunk cache.

Small, deliberately bounded cache for recently fetched Telegram chunks. Keyed by
the stable source identity (chat_id, message_id) plus chunk index, exactly the
"source key + chunk index -> bytes" shape described in the design.

Backed by an OrderedDict; on access entries move to the tail (most recent). The
cache never grows past max_bytes (LRU eviction) and never serves an entry older
than ttl seconds. All access is expected from the asyncio event loop, so no
extra locks are taken.
"""

import time
from collections import OrderedDict
from typing import Any


class BoundedChunkCache:
    def __init__(self, max_bytes: int = 0, ttl_seconds: int = 30):
        if max_bytes < 0:
            raise ValueError("max_bytes must be >= 0")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        self.max_bytes = max_bytes
        self.ttl_seconds = ttl_seconds
        self._store: OrderedDict[Any, tuple[float, bytes]] = OrderedDict()
        self._bytes = 0
        self.enabled = max_bytes > 0

        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.inserts = 0

    def _expire_stale(self, now: float) -> None:
        stale = [k for k, (ts, _) in self._store.items() if now - ts > self.ttl_seconds]
        for k in stale:
            _, blob = self._store.pop(k)
            self._bytes -= len(blob)
            self.evictions += 1

    def get(self, key: Any) -> bytes | None:
        if not self.enabled:
            return None
        now = time.monotonic()
        self._expire_stale(now)
        entry = self._store.get(key)
        if entry is None:
            self.misses += 1
            return None
        ts, blob = entry
        if now - ts > self.ttl_seconds:
            del self._store[key]
            self._bytes -= len(blob)
            self.evictions += 1
            self.misses += 1
            return None
        # refresh recency AND ttl: a cache hit means the entry is hot
        self._store.move_to_end(key)
        self._store[key] = (now, blob)
        self.hits += 1
        return blob

    def put(self, key: Any, blob: bytes) -> None:
        if not self.enabled:
            return
        if len(blob) > self.max_bytes:
            # A single entry larger than the whole cache: nothing useful to do.
            return
        now = time.monotonic()
        prev = self._store.get(key)
        if prev is not None:
            self._bytes -= len(prev[1])
            del self._store[key]
        self._store[key] = (now, blob)
        self._bytes += len(blob)
        self.inserts += 1
        self._evict(now)

    def _evict(self, now: float) -> None:
        while self._bytes > self.max_bytes and self._store:
            # Remove from head = least recently used.
            k, (_, blob) = self._store.popitem(last=False)
            self._bytes -= len(blob)
            self.evictions += 1

    def __contains__(self, key: Any) -> bool:
        return key in self._store if self.enabled else False

    def __len__(self) -> int:
        return len(self._store)

    def clear(self) -> None:
        self._store.clear()
        self._bytes = 0

    @property
    def bytes_used(self) -> int:
        return self._bytes

    def stats(self) -> dict:
        return {
            "enabled": self.enabled,
            "entries": len(self._store),
            "bytes": self._bytes,
            "max_bytes": self.max_bytes,
            "ttl_seconds": self.ttl_seconds,
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "inserts": self.inserts,
        }

    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return (self.hits / total) if total else 0.0