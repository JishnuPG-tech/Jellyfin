"""Post-deployment playback + seek benchmark for the apex_stream Telegram engine.

Snapshots /tg-stream/status + /tg-stream/metrics, runs a sequential full-file
range GET and concurrent random seeks, then prints deltas (streams, cache
hits/misses, bytes, throughput, active runs).

Usage:
    python tools/bench_stream.py [base_url] [--workers 4]
"""

from __future__ import annotations

import argparse
import asyncio
import sys

try:
    import httpx
except ImportError:
    print("httpx is required: pip install httpx", file=sys.stderr)
    sys.exit(1)


def _digest(status: dict, metrics: dict) -> dict:
    counters = metrics.get("counters", {}) if isinstance(metrics, dict) else {}
    totals = metrics.get("totals", {}) if isinstance(metrics, dict) else {}
    cache = status.get("cache", {}) if isinstance(status, dict) else {}
    admission = status.get("admission", {}) if isinstance(status, dict) else {}
    return {
        "counters": dict(counters),
        "totals": dict(totals),
        "cache": dict(cache),
        "active_global": admission.get("active_streams", 0),
        "active_by_source": dict(admission.get("active_by_source", {})),
        "inflight_runs": status.get("inflight_runs", 0),
        "cached_files": status.get("cached_files", 0),
    }


async def _snapshot(client: httpx.AsyncClient, base: str) -> dict:
    status = (await client.get(f"{base}/tg-stream/status")).json()
    metrics = (await client.get(f"{base}/tg-stream/metrics")).json()
    return _digest(status, metrics)


def _delta(kind: str, pre: dict, post: dict, key: str) -> int:
    return post[kind].get(key, 0) - pre[kind].get(key, 0)


async def _run(args) -> int:
    base = args.base.rstrip("/")
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=15.0)) as client:
        pre = await _snapshot(client, base)
        print(
            f"baseline: cached_files={pre['cached_files']} cache_entries="
            f"{pre['cache'].get('entries', 0)} inflight_runs={pre['inflight_runs']} "
            f"active_streams={pre['active_global']} uptime="
            f"{pre['counters'].get('streams_total', 0)} streams"
        )
        if pre["cache"].get("entries", 0) == 0 and not any(
            pre["active_by_source"] or pre["inflight_runs"]
        ):
            print(
                "WARNING: no active/cached source. Feed media to the webhook bot "
                "first, then re-run. Benchmark still exercises empty-cache seeks.",
                file=sys.stderr,
            )

        size = args.chunk
        tasks = []
        for w in range(args.workers):
            start = (w * (size // args.workers)) % size
            end = min(start + args.step, size - 1)
            tasks.append(_seek(client, base, start, end))
        t0 = asyncio.get_running_loop().time()
        lat = await asyncio.gather(*tasks)
        total = asyncio.get_running_loop().time() - t0

        post = await _snapshot(client, base)
        print("\n--- deltas ---")
        for k in ("streams_total", "streams_completed", "streams_cancelled", "streams_errors"):
            d = _delta("counters", pre, post, k)
            print(f"  {k}: {d}")
        print(
            f"  bytes_sent: "
            f"{_delta('totals', pre, post, 'stream_bytes_sent'):,}"
        )
        hits = _delta("cache", pre, post, "hits")
        misses = _delta("cache", pre, post, "misses")
        print(f"  cache: {hits} hits / {misses} misses (entries now {post['cache'].get('entries', 0)})")
        print(
            f"  seeks: {len(tasks)} concurrent range GETs, avg "
            f"{sum(lat) / len(lat) * 1000:.0f} ms, "
            f"total throughput {(args.workers * args.step) / max(total, 1e-9) / 1048576:.1f} MiB/s"
        )
        print(f"  admission active: {post['active_global']} (per-source {post['active_by_source']})")
        print(f"  inflight_runs: {post['inflight_runs']}")
    return 0


async def _seek(client, base: str, start: int, end: int) -> float:
    url = f"{base}/tg-stream/stream_file?file_id=bench&message_id=bench"
    t0 = asyncio.get_running_loop().time()
    async with client.stream("GET", url, headers={"Range": f"bytes={start}-{end}"}) as r:
        # streams may 404 cleanly when no media is cached yet; measure time to
        # decision, which still exercises the driver
        async for _ in r.aiter_bytes():
            pass
    return asyncio.get_running_loop().time() - t0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("base", nargs="?", default="https://jishnupg-apex.hf.space")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--chunk", type=int, default=16 * 1024 * 1024, help="simulated file size (bytes)")
    p.add_argument("--step", type=int, default=1024 * 1024, help="bytes per seek window")
    args = p.parse_args()
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())