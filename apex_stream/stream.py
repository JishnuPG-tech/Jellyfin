"""Stream driver: serves a byte window of a Telegram source to many HTTP
consumers while sharing in-flight Telegram fetches and bounding concurrency.

This module is telegram-free. The caller supplies:

- `resolve_fn(chat_id, message_id) -> FileInfo`   (looks up file metadata)
- `fetch_fn(client, chat_id, message_id, run_start, chunk_count) -> AsyncIterator[(offset_in_run, bytes)]`
  (streams `chunk_count` 1 MiB chunks of the source starting at global chunk
  `run_start`, via one `client`)

`fetch_fn` emits `(offset_in_run, chunk_bytes)`; it must NOT do client
selection — the driver picks and releases clients through the pool.
"""

import asyncio
import logging
import mimetypes
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Optional

from .ranges import (
    ChunkLayout,
    RangeNotSatisfiable,
    RangeRequest,
    chunk_layout,
    clip_last_chunk,
    parse_range,
    trim_first_chunk,
)
from .pool import ClientHandle, NoClientAvailable

logger = logging.getLogger("ApexStream")


@dataclass
class FileInfo:
    size: int
    name: str
    mime_type: Optional[str] = None
    file_id: Optional[str] = None


@dataclass
class StreamPlan:
    status: int  # 200 or 206
    content_type: str
    content_length: int
    content_range: Optional[str]  # "bytes 100-199/1000" for 206, else None
    accept_ranges: str = "bytes"
    chat_id: int = 0
    message_id: int = 0
    file_size: int = 0
    start: int = 0
    end: int = 0
    layout: Optional[ChunkLayout] = field(default=None, init=False)


class StreamDriver:
    def __init__(
        self,
        cfg,
        pool,
        registry,
        metrics,
        resolve_fn: Callable[[int, int], AsyncIterator[FileInfo]],
        fetch_fn: Callable[[object, int, int, int, int], AsyncIterator],
        logger_obj: Optional[logging.Logger] = None,
    ):
        self.cfg = cfg
        self.pool = pool
        self.registry = registry
        self.metrics = metrics
        self.resolve_fn = resolve_fn
        self.fetch_fn = fetch_fn
        self.log = logger_obj or logger

    # --- planning ------------------------------------------------------

    async def plan(
        self,
        chat_id: int,
        message_id: int,
        range_header: Optional[str],
    ) -> StreamPlan:
        info: FileInfo = await self.resolve_fn(chat_id, message_id)
        if info is None:
            raise FileNotFoundError(f"source {chat_id}:{message_id} not resolvable")
        if info.size <= 0:
            raise ValueError(f"source {chat_id}:{message_id} has no bytes")

        content_type = (
            info.mime_type
            or mimetypes.guess_type(info.name)[0]
            or "application/octet-stream"
        )

        rq: Optional[RangeRequest] = parse_range(range_header, info.size)

        if rq is None:
            status, content_length, content_range = 200, info.size, None
            start, end = 0, info.size - 1
        else:
            status, content_length = 206, rq.length
            content_range = f"bytes {rq.start}-{rq.end}/{info.size}"
            start, end = rq.start, rq.end

        plan = StreamPlan(
            status=status,
            content_type=content_type,
            content_length=content_length,
            content_range=content_range,
            chat_id=chat_id,
            message_id=message_id,
            file_size=info.size,
            start=start,
            end=end,
        )
        plan.layout = chunk_layout(
            start,
            end,
            info.size,
            self.cfg.chunk_size,
        )
        return plan

    # --- body generation ----------------------------------------------

    async def generate(
        self,
        plan: StreamPlan,
    ) -> AsyncIterator[bytes]:
        """Yield trimmed body bytes for the planned window.

        Handles: coalesced reads via InFlightRegistry, client selection and
        failover via ClientPool, per-request cancellation, bounded retries.
        """
        layout = plan.layout
        cfg = self.cfg
        source_key = (plan.chat_id, plan.message_id)
        retries_left = cfg.max_telegram_retries
        chunk_index = layout.start_chunk
        emitted_window = 0
        total_needed = layout.total_needed
        skip_leading = layout.skip_leading

        while chunk_index <= layout.end_chunk:
            run_start = (chunk_index // cfg.run_size) * cfg.run_size
            run_end = min(run_start + cfg.run_size - 1, layout.end_chunk)
            run_chunk_count = run_end - run_start + 1
            active_client: Optional[ClientHandle] = None
            try:
                while active_client is None:
                    try:
                        active_client = await self.pool.select()
                    except NoClientAvailable:
                        await asyncio.sleep(0.05)

                factory = self._factory_for(active_client.client, plan.chat_id, plan.message_id)
                run = await self.registry.get_run(
                    source_key, run_start, run_chunk_count, factory
                )
                try:
                    # fast-forward past chunks already delivered (e.g. when
                    # resuming after a failover mid-run)
                    to_skip = chunk_index - run_start
                    for _ in range(to_skip):
                        await run.read_chunk()
                    while chunk_index <= run_end:
                        chunk = await run.read_chunk()
                        chunk_index += 1
                        if emitted_window == 0:
                            chunk = trim_first_chunk(chunk, skip_leading)
                        chunk = clip_last_chunk(chunk, total_needed, emitted_window)
                        if not chunk:
                            continue
                        emitted_window += len(chunk)
                        yield chunk
                finally:
                    # guarantee the pool slot is returned on every exit path
                    # (success, transport error, cancellation, generator close)
                    await run.close()
                    await self.pool.release(active_client.client_id)
                await self.pool.note_success(active_client.client_id)
            except asyncio.CancelledError:
                raise
            except (RangeNotSatisfiable, FileNotFoundError, ValueError):
                raise
            except NoClientAvailable:
                await asyncio.sleep(0.05)
            except Exception as exc:  # noqa: BLE001
                # transport-level telegram failure -> failover to another client
                if (
                    retries_left > 0
                    and not isinstance(exc, (RangeNotSatisfiable, FileNotFoundError, ValueError))
                ):
                    retries_left -= 1
                    await self.pool.note_failure(
                        active_client.client_id, telegram_error=True
                    )
                    self.log.warning(
                        f"[STREAM] failover for {source_key}: {type(exc).__name__}: {exc} "
                        f"({retries_left} retries left)"
                    )
                    continue
                raise

        # success accounting (only reached on clean exhaustion of the window)
        self.metrics.total("bytes_sent", total_needed)
        self.metrics.inc("streams_completed")

    def _factory_for(self, client, chat_id: int, message_id: int):
        fetch = self.fetch_fn

        def _factory(run_start: int, chunk_count: int):
            # fetch_fn is an async generator; calling it must yield an async
            # iterator, not a coroutine, so the registry can `async for` over it.
            return fetch(client, chat_id, message_id, run_start, chunk_count)

        return _factory

    # --- convenience ---------------------------------------------------

    def status_snapshot(self) -> dict:
        return {
            "pool": self.pool.snapshot(),
            "inflight_runs": self.registry.active_run_count(),
            "runs": self.registry.snapshot(),
            "metrics": self.metrics.snapshot(),
            "config": self.cfg.to_dict(),
        }