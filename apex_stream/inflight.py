"""In-flight chunk fetch coalescing.

When Jellyfin / FFmpeg issue overlapping Range requests for the same Telegram
source, the same bytes would otherwise be fetched from Telegram once per HTTP
request. This module fans one Telegram fetch out to many HTTP consumers.

Design
------
A `_Run` is a span of 1 MiB logical chunks of one source (chat_id, message_id)
— identified by (run_start, chunk_count) so consumers requesting the *exact
same span* share a single Telegram fetch. The driver advances in aligned
run_size steps, so two full-file streams coalesce on identical spans; seek
requests that land inside an active span are served from the bounded chunk
cache.

- Sharing: consumers register per-run read positions; a produced chunk is
  delivered to every consumer whose position has not yet passed it.
- Bounded memory: the produce loop holds at most `max_window` unread chunks in
  memory and applies backpressure (waits for the slowest consumer) above that.
- Cancellation isolation: when one consumer leaves its read position is
  dropped; a shared fetch required by other consumers is NOT cancelled. When
  the last consumer leaves, the producer task is cancelled promptly so no
  Telegram bandwidth is wasted.
- Retry/failover: a finished/errored/cancelled run is discarded by the
  registry on the next acquisition, so the caller can resume with a fresh run
  (possibly over a different client) from its chunk offset.
"""

import asyncio
import logging
from collections import OrderedDict
from typing import Any, AsyncIterator, Callable, Optional

logger = logging.getLogger("ApexInflight")


class StreamExhausted(Exception):
    """The producer ended before delivering an awaited chunk (file shorter)."""


class _Run:
    __slots__ = (
        "key", "run_start", "chunk_count", "max_window", "cache", "cache_key",
        "fetch_factory", "window", "positions", "produced", "finished", "error",
        "cond", "task", "consumers", "_closed",
    )

    def __init__(
        self,
        key: tuple,
        run_start: int,
        chunk_count: int,
        max_window: int,
        cache: Any,
        fetch_factory: Callable[[int, int], AsyncIterator[tuple[int, bytes]]],
    ):
        self.key = key
        self.run_start = run_start
        self.chunk_count = chunk_count
        self.max_window = max_window
        self.cache = cache
        self.cache_key = None
        self.fetch_factory = fetch_factory
        self.window: "OrderedDict[int, bytes]" = OrderedDict()
        self.positions: dict[int, int] = {}  # consumer_id -> next offset-in-run
        self.produced = 0
        self.finished = False
        self.error: Optional[Exception] = None
        self.cond = asyncio.Condition()
        self.task: Optional[asyncio.Task] = None
        self.consumers = 0
        self._closed = False

    def _key_global(self, offset_in_run: int) -> tuple:
        source_key, _, _ = self.key
        return (source_key, self.run_start + offset_in_run)

    async def _produce(self):
        try:
            async for offset_in_run, chunk in self.fetch_factory(self.run_start, self.chunk_count):
                async with self.cond:
                    self.window[offset_in_run] = chunk
                    if self.cache is not None:
                        self.cache.put(self._key_global(offset_in_run), chunk)
                    self.produced = max(self.produced, offset_in_run + 1)
                    self._trim()
                    self.cond.notify_all()
                    # Backpressure: keep reading from Telegram only while the
                    # window is below the bound and somebody is still consuming.
                    while (len(self.window) >= self.max_window
                           and not self._closed
                           and self.positions):
                        await self.cond.wait()
                    if self._closed:
                        break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced to consumers
            async with self.cond:
                self.error = exc
                self.finished = True
                self.cond.notify_all()
            logger.warning(f"[INFLIGHT] run {self.key} failed: {type(exc).__name__}: {exc}")
            return
        async with self.cond:
            self.finished = True
            self.cond.notify_all()

    def _trim(self):
        if not self.positions:
            return
        min_pos = min(self.positions.values())
        for off in [o for o in self.window if o < min_pos]:
            self.window.pop(off, None)

    async def start(self, fetch_lock: "asyncio.Semaphore"):
        self.task = asyncio.create_task(self._consume_sem(fetch_lock))

    async def _consume_sem(self, fetch_lock):
        async with fetch_lock:
            await self._produce()

    async def _wait(self, consumer_id: int) -> bytes:
        pos = self.positions.get(consumer_id)
        if pos is None:
            raise StreamExhausted(f"consumer {consumer_id} not registered on run {self.key}")
        async with self.cond:
            while True:
                blob = self.window.get(pos)
                if blob is not None:
                    # chunk stays in window until EVERY consumer passes it;
                    # _trim() below removes it once it is below the slowest
                    # consumer's position.
                    self.positions[consumer_id] = pos + 1
                    self._trim()
                    self.cond.notify_all()
                    return blob
                if self.cache is not None:
                    cached = self.cache.get(self._key_global(pos))
                    if cached is not None:
                        self.positions[consumer_id] = pos + 1
                        self._trim()
                        self.cond.notify_all()
                        return cached
                if self.error:
                    self.positions.pop(consumer_id, None)
                    self._trim()
                    self.cond.notify_all()
                    raise self.error
                if self.finished and pos >= self.produced:
                    self.positions.pop(consumer_id, None)
                    self._trim()
                    self.cond.notify_all()
                    raise StreamExhausted(
                        f"chunk {self.run_start + pos} not produced by run {self.key}"
                    )
                await self.cond.wait()

    async def register_consumer(self, consumer_id: int, offset_in_run: int) -> None:
        async with self.cond:
            self.positions[consumer_id] = offset_in_run
            self.consumers += 1
            self.cond.notify_all()

    async def unregister_consumer(self, consumer_id: int) -> None:
        async with self.cond:
            self.positions.pop(consumer_id, None)
            self.consumers = max(0, self.consumers - 1)
            self._trim()
            self.cond.notify_all()
            if self.consumers == 0 and self.task and not self.task.done() and not self._closed:
                self._closed = True
                self.task.cancel()
            elif self.finished and self.consumers == 0:
                self._closed = True


class SourceRun:
    """Per-consumer handle onto a shared in-flight Telegram fetch.

    A consumer reads chunks strictly in order via read_chunk(); the run tracks
    its position. close() must be called exactly once (use try/finally).
    """

    def __init__(self, run: "_Run", consumer_id: int, start_offset: int):
        self._run = run
        self.consumer_id = consumer_id
        self.offset = start_offset

    async def read_chunk(self) -> bytes:
        """Read the next chunk at this consumer's tracked position."""
        return await self._run._wait(self.consumer_id)

    @property
    def run_start(self) -> int:
        return self._run.run_start

    @property
    def chunk_count(self) -> int:
        return self._run.chunk_count

    @property
    def finished(self) -> bool:
        return self._run.finished

    @property
    def current_offset(self) -> int:
        return self._run.positions.get(self.consumer_id, 0)

    async def close(self) -> None:
        await self._run.unregister_consumer(self.consumer_id)
        self.consumer_id = -1


class InFlightRegistry:
    """Tracks active source runs and coalesces overlapping chunk fetches.

    Keying: (source_key, run_start, chunk_count). Consumers requesting the
    identical span share one Telegram fetch.
    """

    def __init__(
        self,
        cache: Any = None,
        run_size: int = 16,
        max_window: int = 4,
        max_inflight_runs: int = 48,
        logger_obj: Optional[logging.Logger] = None,
    ):
        self.cache = cache
        self.run_size = run_size
        self.max_window = max_window
        self.max_inflight_runs = max_inflight_runs
        self.logger = logger_obj or logger
        self._runs: dict = {}
        self._consumer_seq = 0
        self._fetch_semaphore = asyncio.Semaphore(max_inflight_runs)

    def _run_key(self, source_key, run_start, chunk_count) -> tuple:
        return (source_key, run_start, chunk_count)

    def get_cached_chunk(self, source_key: tuple, chunk_index: int) -> Optional[bytes]:
        if self.cache is None:
            return None
        return self.cache.get((source_key, chunk_index))

    def _prune(self):
        """Drop runs that are finished/errored/cancelled and no longer consumed."""
        stale = [
            key
            for key, run in self._runs.items()
            if (run.finished or run.error is not None or run._closed) and run.consumers == 0
        ]
        for key in stale:
            del self._runs[key]

    async def get_run(
        self,
        source_key: tuple,
        run_start: int,
        chunk_count: int,
        fetch_factory: Callable[[int, int], AsyncIterator[tuple[int, bytes]]],
    ) -> SourceRun:
        """Acquire (or join) the run covering chunks [run_start, run_start+chunk_count).

        fetch_factory(run_start, chunk_count) must return an async iterator of
        (offset_in_run, bytes) pairs. It is only invoked when a NEW run must be
        started (i.e. no active shared run exists yet).
        """
        key = self._run_key(source_key, run_start, chunk_count)
        run = self._runs.get(key)
        if run is None or run.finished or run.error is not None or run._closed:
            if run is not None:
                del self._runs[key]
            run = _Run(
                key=(source_key, run_start, chunk_count),
                run_start=run_start,
                chunk_count=chunk_count,
                max_window=self.max_window,
                cache=self.cache,
                fetch_factory=fetch_factory,
            )
            self._runs[key] = run
            await run.start(self._fetch_semaphore)
        self._consumer_seq += 1
        cid = self._consumer_seq
        await run.register_consumer(cid, 0)
        return SourceRun(run, cid, run_start)

    def active_run_count(self) -> int:
        self._prune()
        return len(self._runs)

    def snapshot(self) -> list:
        self._prune()
        out = []
        for key, run in self._runs.items():
            source_key, run_start, chunk_count = key
            out.append(
                {
                    "key": f"{source_key[0]}:{source_key[1]}/ch{run_start}+{chunk_count}",
                    "produced": run.produced,
                    "finished": run.finished,
                    "consumers": run.consumers,
                    "window": len(run.window),
                    "error": str(run.error) if run.error else None,
                }
            )
        return out