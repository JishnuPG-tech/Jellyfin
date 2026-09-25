"""Apex streaming core.

Telegram-free, dependency-light modules that implement the FileToLink-inspired
streaming architecture for Apex:

- ranges: RFC 7233 Range parsing / byte layout calculation
- cache:  bounded LRU+TTL hot-chunk cache
- inflight: shared in-flight chunk fetch coalescing (ref-counted, cancellable)
- pool: multi-client Telegram client pool with health + least-loaded selection
- metrics: lightweight streaming counters
- stream: end-to-end range streaming orchestration (retry/failover aware)

The pyrogram/aiohttp wiring lives in tg_streamer.py; everything importable here
is unit-testable without a Telegram connection.
"""

from .ranges import (
    RangeRequest,
    RangeNotSatisfiable,
    parse_range,
    chunk_layout,
    CHUNK_SIZE_DEFAULT,
)
from .cache import BoundedChunkCache
from .metrics import StreamMetrics
from .pool import ClientHandle, ClientPool, NoClientAvailable
from .inflight import InFlightRegistry, SourceRun, StreamExhausted
from .stream import FileInfo, StreamPlan, StreamDriver
from .config import Config

__all__ = [
    "RangeRequest",
    "RangeNotSatisfiable",
    "parse_range",
    "chunk_layout",
    "CHUNK_SIZE_DEFAULT",
    "BoundedChunkCache",
    "StreamMetrics",
    "ClientHandle",
    "ClientPool",
    "NoClientAvailable",
    "InFlightRegistry",
    "SourceRun",
    "StreamExhausted",
    "FileInfo",
    "StreamPlan",
    "StreamDriver",
    "Config",
]