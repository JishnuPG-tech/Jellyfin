"""Unit tests for apex_stream.inflight — coalescing, backpressure, cancel."""

import asyncio

import pytest

from apex_stream.cache import BoundedChunkCache
from apex_stream.inflight import InFlightRegistry, StreamExhausted


def _chunk(index, size=1024):
    # deterministic bytes per chunk
    n = size
    return (b"%08d-" % index) * (n // 9) + b"Z" * (n % 9)


async def _fake_factory(total_chunks, emit_all=True, fail_at=None, delay=0.0):
    """Returns a fetch_factory: (run_start, chunk_count) -> async gen of (offset, bytes)."""

    def factory(run_start, chunk_count):
        async def _gen():
            for i in range(chunk_count):
                global_index = run_start + i
                if global_index >= total_chunks:
                    break
                if fail_at is not None and i == fail_at:
                    raise ConnectionError(f"fail at {i}")
                if delay:
                    await asyncio.sleep(delay)
                yield (i, _chunk(global_index))

        return _gen()

    return factory


async def _drain(run, count):
    out = b""
    for _ in range(count):
        out += await run.read_chunk()
    return out


class TestRegistryBasics:
    async def test_single_consumer(self):
        registry = InFlightRegistry(run_size=16, max_window=4)
        src = (100, 200)
        factory = await _fake_factory(total_chunks=5)
        run = await registry.get_run(src, 0, 5, factory)
        try:
            data = await _drain(run, 5)
            assert len(data) == 5 * 1024
        finally:
            await run.close()
        # run finished after drain; acquiring again gives a fresh run
        r2 = await registry.get_run(src, 0, 5, factory)
        await r2.close()
        assert registry.active_run_count() == 0  # pruned after finished+closed

    async def test_two_consumers_share_one_fetch(self):
        """fetch_factory is invoked exactly once when two consumers want the same span."""
        calls = []

        async def _fake_chunks(chunk_count, run_start):
            for i in range(chunk_count):
                yield (i, _chunk(run_start + i))

        def factory(run_start, chunk_count):
            calls.append((run_start, chunk_count))
            return _fake_chunks(chunk_count, run_start)

        registry = InFlightRegistry(run_size=16, max_window=4)
        src = (1, 2)
        r1 = await registry.get_run(src, 0, 4, factory)
        r2 = await registry.get_run(src, 0, 4, factory)
        try:
            d1, d2 = await asyncio.gather(_drain(r1, 4), _drain(r2, 4))
            assert d1 == d2
            assert len(calls) == 1
        finally:
            await r1.close()
            await r2.close()

    async def test_cache_served_across_spans(self):
        """A second run reuses chunks already in the shared cache."""
        cache = BoundedChunkCache(max_bytes=64 * 1024, ttl_seconds=60)
        registry = InFlightRegistry(cache=cache, run_size=16, max_window=4)

        async def factory(run_start, chunk_count):
            for i in range(chunk_count):
                yield (i, _chunk(run_start + i))

        src = (1, 2)
        r1 = await registry.get_run(src, 0, 2, factory)
        await _drain(r1, 2)
        await r1.close()

        # New span covering chunk 1 only; chunk data must come from cache
        # (producer still runs, but the consumer gets it instantly via cache).
        r2 = await registry.get_run(src, 1, 1, factory)
        try:
            data = await r2.read_chunk()
            assert data == _chunk(1)
        finally:
            await r2.close()


class TestBackpressure:
    async def test_producer_blocks_when_window_full(self):
        """The fetch shouldn't run away beyond the bound while nobody reads."""
        semaphore = asyncio.Semaphore()
        registry = InFlightRegistry(run_size=16, max_window=2)
        src = (3, 4)
        produced = []

        async def slow_factory(run_start, chunk_count):
            for i in range(chunk_count):
                produced.append(i)
                yield (i, _chunk(i))
                await asyncio.sleep(0.001)

        run = await registry.get_run(src, 0, 5, slow_factory)
        try:
            await asyncio.sleep(0.01)
            # producer should be blocked at max_window=2, not 5
            assert len(produced) <= 3, f"producer over-ran: {len(produced)}"
            total = b""
            for _ in range(5):
                total += await run.read_chunk()
            assert len(total) == 5 * 1024
        finally:
            await run.close()


class TestCancellation:
    async def test_close_cancels_producer_task(self):
        registry = InFlightRegistry(run_size=16, max_window=2)
        src = (7, 8)

        async def endless_factory(run_start, chunk_count):
            i = 0
            while True:
                yield (i, _chunk(run_start + i))
                i += 1
                await asyncio.sleep(0.001)

        run = await registry.get_run(src, 0, 10000, endless_factory)
        await run.read_chunk()
        await run.read_chunk()
        # close should cancel the underlying producer
        await run.close()
        assert run._run._closed is True or run._run.finished

    async def test_one_consumer_leaving_keeps_others(self):
        registry = InFlightRegistry(run_size=16, max_window=2)
        src = (9, 10)

        async def factory(run_start, chunk_count):
            for i in range(3):
                yield (i, _chunk(run_start + i))

        r1 = await registry.get_run(src, 0, 3, factory)
        r2 = await registry.get_run(src, 0, 3, factory)
        data1 = await r1.read_chunk()
        await r1.close()  # producer should stay alive for r2
        assert not r2._run._closed
        data2 = await _drain(r2, 3)
        assert len(data2) == 3 * 1024
        assert data1 == _chunk(0)
        await r2.close()


class TestFailover:
    async def test_error_surfaced_then_fresh_run(self):
        registry = InFlightRegistry(run_size=16, max_window=2)
        src = (11, 12)

        async def flaky_factory(run_start, chunk_count):
            for i in range(chunk_count):
                if i == 2:
                    raise ConnectionError("boom")
                yield (i, _chunk(run_start + i))

        run = await registry.get_run(src, 0, 5, flaky_factory)
        # chunks 0,1 are deliverable; the error surfaces on the 3rd read
        assert await run.read_chunk() == _chunk(0)
        assert await run.read_chunk() == _chunk(1)
        with pytest.raises(ConnectionError):
            await run.read_chunk()
        await run.close()
        # acquiring a new run replaces the errored one
        run2 = await registry.get_run(src, 0, 5, flaky_factory)
        assert run2._run is not run._run
        await run2.close()

    async def test_stream_exhausted_for_short_source(self):
        registry = InFlightRegistry(run_size=16, max_window=2)
        src = (13, 14)

        async def short_factory(run_start, chunk_count):
            for i in range(2):  # only 2 chunks available
                yield (i, _chunk(run_start + i))

        run = await registry.get_run(src, 0, 5, short_factory)
        with pytest.raises(StreamExhausted):
            await _drain(run, 5)
        await run.close()