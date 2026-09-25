"""Unit tests for apex_stream.stream — end-to-end range streaming."""

import asyncio

import pytest

from apex_stream.cache import BoundedChunkCache
from apex_stream.config import Config
from apex_stream.inflight import InFlightRegistry
from apex_stream.metrics import StreamMetrics
from apex_stream.pool import ClientPool
from apex_stream.ranges import RangeNotSatisfiable
from apex_stream.stream import FileInfo, StreamDriver


class _FakeTelegram:
    """Simulates one Pyrogram client exposing stream_media()-like reads."""

    def __init__(self, file_size, fail_call_count=0, chunk_size=1024 * 1024):
        self.file_size = file_size
        self.fail_call_count = fail_call_count  # fail first N read_range calls
        self.chunk_size = chunk_size
        self.fetch_count = 0

    async def read_range(self, offset_chunk, chunk_count):
        if self.fetch_count < self.fail_call_count:
            self.fetch_count += 1
            raise ConnectionError("telegram transport down")
        for i in range(chunk_count):
            self.fetch_count += 1
            gidx = offset_chunk + i
            if gidx >= (self.file_size + self.chunk_size - 1) // self.chunk_size:
                return
            # deterministic bytes: identify chunk by global index
            b = bytearray(self.chunk_size)
            for j in range(0, self.chunk_size, 64):
                tag = f"c{gidx:06d}".encode()
                b[j : j + len(tag)] = tag
            yield (i, bytes(b))


def _make_driver(
    file_size=10 * 1024 * 1024,
    failures=0,
    cache_mb=32,
    run_size=8,
    max_retries=1,
    num_clients=1,
):
    cfg = Config(
        {
            "APEX_STREAM_CHUNK_SIZE": "1048576",
            "APEX_STREAM_RUN_CHUNKS": str(run_size),
            "APEX_STREAM_RUN_WINDOW": "4",
            "APEX_STREAM_MAX_GLOBAL": "32",
            "APEX_STREAM_MAX_PER_CLIENT": str(num_clients * 4),
            "APEX_STREAM_MAX_PER_SOURCE": "8",
            "APEX_STREAM_MAX_INFLIGHT": "16",
            "APEX_STREAM_CACHE_ENABLED": "true",
            "APEX_STREAM_CACHE_MB": str(cache_mb),
            "APEX_STREAM_CACHE_TTL_SECONDS": "60",
            "APEX_STREAM_EXTRA_TOKENS": "",
            "APEX_TELEGRAM_CLIENT_COOLDOWN": "5",
            "APEX_TELEGRAM_FAILURE_THRESHOLD": "2",
            "APEX_TELEGRAM_MAX_RETRIES": str(max_retries),
            "APEX_TELEGRAM_MAX_CONCURRENT": "4",
        }
    )
    pool = ClientPool(
        max_streams_per_client=cfg.max_per_client,
        failure_threshold=cfg.client_failure_threshold,
        cooldown_seconds=cfg.client_cooldown_seconds,
    )
    chunk_size = cfg.chunk_size
    total_chunks = (file_size + chunk_size - 1) // chunk_size

    async def resolve_fn(chat_id, message_id):
        return FileInfo(size=file_size, name=None, mime_type="video/mp4")

    async def fetch_fn(client, chat_id, message_id, run_start, chunk_count):
        # client is _FakeTelegram
        async for off, chunk in client.read_range(run_start, chunk_count):
            yield (off, chunk)

    cache = BoundedChunkCache(
        max_bytes=cfg.cache_size_bytes if cache_mb else 0,
        ttl_seconds=cfg.cache_ttl_seconds,
    )
    registry = InFlightRegistry(
        cache=cache, run_size=cfg.run_size, max_window=cfg.run_window
    )
    metrics = StreamMetrics()
    driver = StreamDriver(cfg, pool, registry, metrics, resolve_fn, fetch_fn)
    return driver, pool, registry, cache, metrics, file_size


def _expected_bytes(global_chunk, chunk_size):
    b = bytearray(chunk_size)
    for j in range(0, chunk_size, 64):
        tag = f"c{global_chunk:06d}".encode()
        b[j : j + len(tag)] = tag
    return bytes(b)


class TestPlan:
    async def test_no_range_full_file(self):
        driver, *_ = _make_driver()
        plan = await driver.plan(1, 2, None)
        assert plan.status == 200
        assert plan.content_length == plan.file_size
        assert plan.content_range is None
        assert plan.layout.total_needed == plan.file_size

    async def test_partial_range_206(self):
        driver, *_ = _make_driver()
        plan = await driver.plan(1, 2, "bytes=0-1048575")
        assert plan.status == 206
        assert plan.content_length == 1048576
        assert plan.content_range == f"bytes 0-1048575/{10 * 1024 * 1024}"

    async def test_range_past_eof_416(self):
        driver, *_ra = _make_driver()
        with pytest.raises(RangeNotSatisfiable):
            await driver.plan(1, 2, "bytes=99999999-")


class TestGenerate:
    async def test_full_file_stream(self):
        driver, pool, *_ = _make_driver(file_size=3 * 1024 * 1024)
        await pool.register(1, _FakeTelegram(file_size=3 * 1024 * 1024))
        plan = await driver.plan(1, 2, None)
        chunks = [c async for c in driver.generate(plan)]
        body = b"".join(chunks)
        assert len(body) == plan.file_size == 3 * 1024 * 1024

    async def test_range_stream_trims_edges(self):
        driver, pool, *_ = _make_driver()
        await pool.register(1, _FakeTelegram(file_size=10 * 1024 * 1024))
        # seek into the middle of chunk 0 and stop mid-chunk 2
        cs = 1024 * 1024
        start, end = 5000, 2 * cs + 4000
        plan = await driver.plan(1, 2, f"bytes={start}-{end}")
        body = b"".join([c async for c in driver.generate(plan)])
        assert len(body) == end - start + 1
        # first bytes belong to chunk 0 at offset 5000
        expected_first = _expected_bytes(0, cs)[5000:5000 + 64]
        assert body[:64] == expected_first
        # trailing bytes end exactly at `end` inside chunk 2
        within_chunk2 = end - 2 * cs
        expected_last = _expected_bytes(2, cs)[within_chunk2 - 63 : within_chunk2 + 1]
        assert body[-64:] == expected_last

    async def test_two_clients_stream_different_ranges(self):
        driver, pool, *_ = _make_driver(num_clients=2)
        await pool.register(1, _FakeTelegram(file_size=10 * 1024 * 1024))
        await pool.register(2, _FakeTelegram(file_size=10 * 1024 * 1024))
        p1 = await driver.plan(1, 2, "bytes=0-1048575")
        p2 = await driver.plan(1, 2, "bytes=1048576-2097151")
        b1, b2 = await asyncio.gather(
            _collect(driver, p1), _collect(driver, p2)
        )
        assert len(b1) == 1048576
        assert len(b2) == 1048576
        assert b1 == _expected_bytes(0, 1024 * 1024)
        assert b2 == _expected_bytes(1, 1024 * 1024)


async def _collect(driver, plan):
    return b"".join([c async for c in driver.generate(plan)])


class TestFailover:
    async def test_retry_after_transport_failure(self):
        driver, pool, registry, *_ = _make_driver(
            file_size=8 * 1024 * 1024, max_retries=2, num_clients=1
        )
        fake = _FakeTelegram(file_size=8 * 1024 * 1024, fail_call_count=1)
        await pool.register(1, fake)
        plan = await driver.plan(1, 2, None)
        chunks = [c async for c in driver.generate(plan)]
        body = b"".join(chunks)
        assert len(body) == 8 * 1024 * 1024  # retry recovered the full file
        assert pool.snapshot()["clients"][0]["consecutive_failures"] == 0

    async def test_retries_exhausted_raise(self):
        driver, pool, *_ = _make_driver(
            file_size=8 * 1024 * 1024, max_retries=1, num_clients=1
        )
        fake = _FakeTelegram(file_size=8 * 1024 * 1024, fail_call_count=999)
        await pool.register(1, fake)
        plan = await driver.plan(1, 2, None)
        with pytest.raises(ConnectionError):
            _ = [c async for c in driver.generate(plan)]

    async def test_cancellation_releases_client(self):
        driver, pool, *_ = _make_driver(num_clients=1)
        await pool.register(1, _FakeTelegram(file_size=4 * 1024 * 1024))
        plan = await driver.plan(1, 2, None)

        async def consume_until_cancel():
            gen = driver.generate(plan)
            async for chunk in gen:
                break  # simulate early FFmpeg stop
            await gen.aclose()

        await consume_until_cancel()
        # the client's stream counters should be back to 0
        assert pool.snapshot()["clients"][0]["active_streams"] == 0
        assert pool.snapshot()["clients"][0]["active_requests"] == 0