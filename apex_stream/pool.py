"""Telegram client pool with health tracking and least-loaded selection.

Inspired by FileToLink's workloads[client] approach but made idiomatic for an
asyncio/Pyrogram process:

- each client tracks active request/stream workload, consecutive failures,
  last failure time and a cooldown window
- selection always picks the healthy client with the lowest active workload
  that is still below its per-client concurrency ceiling
- a client that repeatedly fails goes into cooldown and is skipped; after the
  cooldown elapses it is re-checked on the next selection pass
- cancellations are reported as neutral (not telegram errors), so an FFmpeg
  stop / client disconnect never trips the circuit breaker

Telegram is NOT imported here: the `client` payload is opaque. That keeps the
pool unit-testable.
"""

import time
import asyncio
from dataclasses import dataclass, field
from typing import Any, Optional

# Client health states
HEALTHY = "healthy"
DEGRADED = "degraded"
COOLDOWN = "cooldown"
UNAVAILABLE = "unavailable"


class NoClientAvailable(Exception):
    """Raised when every client is at capacity or in cooldown."""


@dataclass
class ClientHandle:
    client_id: int
    client: Any
    max_streams: int = 8
    state: str = HEALTHY
    active_requests: int = 0
    active_streams: int = 0
    consecutive_failures: int = 0
    last_failure_ts: float = 0.0
    cooldown_until: float = 0.0
    created_ts: float = field(default_factory=time.monotonic)

    def is_available(self, now: float) -> bool:
        if self.state == UNAVAILABLE:
            return False
        if self.state == COOLDOWN:
            return now >= self.cooldown_until
        return True

    def current_load(self) -> tuple[int, int]:
        # primary sort: active streams; tie-break: total active requests
        return (self.active_streams, self.active_requests)


class ClientPool:
    def __init__(
        self,
        max_streams_per_client: int = 8,
        failure_threshold: int = 3,
        cooldown_seconds: int = 30,
        logger: Optional[Any] = None,
    ):
        self.max_streams_per_client = max_streams_per_client
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.logger = logger
        self._clients: dict[int, ClientHandle] = {}
        self._lock = asyncio.Lock()

    def __len__(self) -> int:
        return len(self._clients)

    async def register(self, client_id: int, client: Any, *, ready: bool = True) -> ClientHandle:
        async with self._lock:
            handle = ClientHandle(
                client_id=client_id,
                client=client,
                max_streams=self.max_streams_per_client,
                state=HEALTHY if ready else DEGRADED,
            )
            self._clients[client_id] = handle
            return handle

    async def unregister(self, client_id: int) -> None:
        async with self._lock:
            self._clients.pop(client_id, None)

    async def set_state(self, client_id: int, state: str) -> None:
        async with self._lock:
            handle = self._clients.get(client_id)
            if handle:
                handle.state = state

    def _eligible(self, now: float) -> list[ClientHandle]:
        out = []
        for handle in self._clients.values():
            if not handle.is_available(now):
                continue
            # re-open a client whose cooldown has elapsed
            if handle.state == COOLDOWN:
                handle.state = DEGRADED
            if handle.active_streams >= handle.max_streams:
                continue
            out.append(handle)
        return out

    async def select(self) -> ClientHandle:
        """Pick the least-loaded eligible client, or raise NoClientAvailable."""
        now = time.monotonic()
        async with self._lock:
            eligible = self._eligible(now)
            if not eligible:
                raise NoClientAvailable(
                    "all Telegram clients at capacity or in cooldown"
                )
            chosen = min(eligible, key=lambda h: h.current_load())
            chosen.active_streams += 1
            chosen.active_requests += 1
            return chosen

    async def release(self, client_id: int) -> None:
        async with self._lock:
            handle = self._clients.get(client_id)
            if not handle:
                return
            handle.active_streams = max(0, handle.active_streams - 1)
            handle.active_requests = max(0, handle.active_requests - 1)

    async def note_success(self, client_id: int) -> None:
        async with self._lock:
            handle = self._clients.get(client_id)
            if not handle:
                return
            handle.consecutive_failures = 0
            if handle.state == COOLDOWN:
                handle.state = DEGRADED
            elif handle.state == DEGRADED:
                handle.state = HEALTHY

    async def note_failure(self, client_id: int, *, telegram_error: bool) -> None:
        async with self._lock:
            handle = self._clients.get(client_id)
            if not handle:
                return
            if not telegram_error:
                # Cancellation / client-disconnect / FFmpeg stop: not a transport
                # failure, must not trip cooldown.
                return
            handle.consecutive_failures += 1
            handle.last_failure_ts = time.monotonic()
            if handle.consecutive_failures >= self.failure_threshold:
                handle.state = COOLDOWN
                handle.cooldown_until = handle.last_failure_ts + self.cooldown_seconds
                if self.logger:
                    self.logger.warning(
                        f"[POOL] client={client_id} entered COOLDOWN for {self.cooldown_seconds}s "
                        f"({handle.consecutive_failures} failures)"
                    )
            elif handle.state == HEALTHY:
                handle.state = DEGRADED

    def snapshot(self) -> dict:
        now = time.monotonic()
        return {
            "total": len(self._clients),
            "max_streams_per_client": self.max_streams_per_client,
            "clients": [
                {
                    "id": h.client_id,
                    "state": h.state,
                    "active_streams": h.active_streams,
                    "active_requests": h.active_requests,
                    "consecutive_failures": h.consecutive_failures,
                    "last_failure_ts": round(h.last_failure_ts, 2),
                    "cooldown_remaining": max(0.0, h.cooldown_until - now),
                    "max_streams": h.max_streams,
                }
                for h in self._clients.values()
            ],
        }