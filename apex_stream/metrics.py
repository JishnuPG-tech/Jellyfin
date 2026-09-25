"""Lightweight streaming metrics.

Small, allocation-light counters used to aggregate per-stream and per-fetch
statistics. Not a monitoring framework: just a bounded dict of monotonic
counters plus a tiny ring buffer of recent per-stream records for the /status
endpoint. All mutation happens on the asyncio event loop thread.
"""

import time
from collections import deque
from typing import Any


class StreamMetrics:
    def __init__(self, recent_capacity: int = 200):
        self.started_at = time.monotonic()
        self._counters: dict[str, int] = {}
        self._totals: dict[str, float] = {}
        self._recent: deque[dict[str, Any]] = deque(maxlen=recent_capacity)

    def inc(self, name: str, n: int = 1) -> None:
        self._counters[name] = self._counters.get(name, 0) + n

    def total(self, name: str, value: float) -> None:
        self._totals[name] = self._totals.get(name, 0) + value

    def record_stream(self, entry: dict[str, Any]) -> None:
        self._recent.append(entry)
        # keep summary counters rolling
        self.inc("streams_total")
        if entry.get("status") == "cancelled":
            self.inc("streams_cancelled")
        elif entry.get("error"):
            self.inc("streams_errors")
        else:
            self.inc("streams_completed")
        dur = entry.get("duration", 0.0)
        self.total("stream_duration_seconds", dur)
        sent = entry.get("bytes_sent", 0)
        self.total("stream_bytes_sent", sent)

    def snapshot(self) -> dict[str, Any]:
        uptime = time.monotonic() - self.started_at
        return {
            "uptime_seconds": round(uptime, 1),
            "counters": dict(self._counters),
            "totals": dict(self._totals),
            "recent_streams": list(self._recent),
        }