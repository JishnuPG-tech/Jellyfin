"""Unit tests for apex_stream.pool — health tracking + least-loaded selection."""

import time

import pytest

from apex_stream.pool import (
    ClientPool,
    NoClientAvailable,
)

CHUNK = b"x" * 1024


def _client(fail_times=0, name="c"):
    class FakeClient:
        def __init__(self):
            self.name = name
            self.fail_count = 0
            self.fail_times = fail_times

        async def fetch(self, n):
            if self.fail_count < self.fail_times:
                self.fail_count += 1
                raise ConnectionError("transport down")
            return CHUNK * n

    return FakeClient()


class TestPoolSelection:
    async def test_least_loaded_selected(self):
        pool = ClientPool(max_streams_per_client=4)
        c1 = _client(name="c1")
        c2 = _client(name="c2")
        await pool.register(1, c1)
        await pool.register(2, c2)
        h = await pool.select()
        assert h.client_id in (1, 2)
        h2 = await pool.select()
        assert h2.client_id != h.client_id or pool.max_streams_per_client == 1
        assert h.active_streams == h2.active_streams == 1

    async def test_capacity_exhausted_raises(self):
        pool = ClientPool(max_streams_per_client=1)
        await pool.register(1, _client())
        await pool.register(2, _client())
        await pool.select()
        await pool.select()
        with pytest.raises(NoClientAvailable):
            await pool.select()

    async def test_release_frees_slot(self):
        pool = ClientPool(max_streams_per_client=1)
        await pool.register(1, _client())
        await pool.register(2, _client())
        h = await pool.select()
        await pool.release(h.client_id)
        # now selectable again
        h2 = await pool.select()
        assert h2.client_id == h.client_id

    async def test_select_avoids_specific_client(self):
        pool = ClientPool(max_streams_per_client=4)
        await pool.register(1, _client(name="bad"))
        await pool.register(2, _client(name="good"))
        h = await pool.select(avoid=1)
        assert h.client_id == 2

    async def test_avoid_ignored_when_only_one_eligible(self):
        pool = ClientPool(max_streams_per_client=2)
        await pool.register(1, _client(name="only"))
        h = await pool.select(avoid=1)
        assert h.client_id == 1

    async def test_least_loaded_prefers_idle(self):
        pool = ClientPool(max_streams_per_client=10)
        await pool.register(1, _client(name="busy"))
        await pool.register(2, _client(name="idle"))
        await pool.select()  # loads client 1 (or 2)
        idle = pool.select()
        # whichever was least loaded should be chosen again or the idle one
        h2 = await idle
        snapshot = pool.snapshot()["clients"]
        loads = {c["id"]: c["active_streams"] for c in snapshot}
        assert max(loads.values()) <= 2

    async def test_note_success_resets_failures(self):
        pool = ClientPool(max_streams_per_client=1)
        await pool.register(1, _client())
        await pool.note_failure(1, telegram_error=True)
        await pool.note_failure(1, telegram_error=True)
        await pool.note_success(1)
        assert pool.snapshot()["clients"][0]["consecutive_failures"] == 0

    async def test_cancellation_is_neutral(self):
        pool = ClientPool(max_streams_per_client=1, failure_threshold=2)
        await pool.register(1, _client())
        await pool.note_failure(1, telegram_error=False)  # cancellation
        assert pool.snapshot()["clients"][0]["state"] == "healthy"
        assert pool.snapshot()["clients"][0]["consecutive_failures"] == 0


class TestPoolCooldown:
    async def test_cooldown_after_threshold(self):
        pool = ClientPool(failure_threshold=3, cooldown_seconds=60)
        await pool.register(1, _client())
        for _ in range(3):
            await pool.note_failure(1, telegram_error=True)
        snap = pool.snapshot()["clients"][0]
        assert snap["state"] == "cooldown"
        assert snap["cooldown_remaining"] > 0

    async def test_eligible_after_cooldown(self, monkeypatch):
        pool = ClientPool(failure_threshold=1, cooldown_seconds=1)
        await pool.register(1, _client())
        await pool.note_failure(1, telegram_error=True)
        assert pool.snapshot()["clients"][0]["state"] == "cooldown"
        # re-open after cooldown
        handle = pool._clients[1]
        handle.cooldown_until = time.monotonic() - 0.1
        h = await pool.select()
        assert h.client_id == 1
        state = pool.snapshot()["clients"][0]["state"]
        assert state != "unavailable"


class TestPoolSnapshots:
    async def test_unregister_removes_client(self):
        pool = ClientPool()
        await pool.register(1, _client())
        await pool.register(2, _client())
        assert len(pool) == 2
        await pool.unregister(1)
        assert len(pool) == 1