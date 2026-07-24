# Phase 7 — Streaming: WebSocket vs SSE Analysis

## TL;DR

We picked **WebSocket** for the terminal stream. SSE was considered for
output-only streaming but it cannot satisfy "user types a command and the
terminal echoes the response" because it is **one-way (server → client)**.

## What each option gives us

### WebSocket (chosen)

| Property | Value |
|---------|-------|
| Direction | Full-duplex |
| Latency | < 5 ms median on intra-DC HF sockets |
| Backpressure | `send` fails or stalls — we wrap in `asyncio.Queue` to drop oldest |
| Headers | One `Upgrade: websocket` |
| Standard library | `websockets` (Python), `WebSocket` (browser) |
| Reconnect logic | Custom (browser-side, 3s backoff, capped at 8s) |
| Multiplex | One WS carries resize, ping, snapshot, byte-stream |

### SSE (rejected)

| Property | Value |
|---------|-------|
| Direction | Server → client ONLY |
| Latency | Same as WS |
| Backpressure | Client closes connection on overflow — terminal drops |
| Headers | `Content-Type: text/event-stream` |
| Standard library | `EventSource` (browser); would need a 2nd WS anyway for input |
| Conclusion | Inadequate for an interactive terminal — would have to layer two transports |

## Why we still use SSE-aware streaming elsewhere

The OpenCode chat UI streams SSE responses (e.g. token-by-token chat
completion). Our **HTTP reverse proxy** is already **streaming-aware**:
when the upstream replies with `Content-Type: text/event-stream` we proxy
the bytes through `StreamingResponse` and call `aiter_bytes()` so chunks
arrive at the browser without buffering. See `backend/app/main.py`
(`_opencode_proxy`).

## How the terminal actually flows

```
Browser (xterm.js)                   FastAPI (uvicorn worker)
==================                   =========================
key downs  →  text/binary frame  →   backend/app/main.py:/terminal/ws
                                        │
                                        ▼
                                   PtyService.write(bytes)
                                        │
                                        ▼
                                   os.write(master_fd, bytes)
                                        │
                                        ▼
                                   /bin/bash (slave pty)
                                        │
                                        ▼ stdout
                                   master_fd
                                        │
                                   PtyService._read_loop  ─► subscribers
                                        │
   binary frame (raw bytes)  ◄───────────┘ (asyncio.Queue)
   term.write(u8)         ◄──── browser xterm.js paints it
```

## Backpressure strategy

* Subscriber queue size: 512 messages.
* On `queue.put_nowait` failure, drop the **oldest** message rather than
  the newest, so the user always sees the most recent output.
* Senders (`ws.send_text` / `ws.send_bytes`) are awaited; if a slow
  client stalls, the reader loop is bounded by the asyncio.Queue.

## Reconnect strategy

* Browser uses exponential backoff (250 ms → 8 s max).
* On reconnect, server replays the last 16 KB of scrollback so context is
  restored.
* Browser also re-sends `resize:<cols>:<rows>` so the kernel TTY matches
  the new viewport immediately.

## Latency budget (typical intra-DC HF)

| Stage | µs |
|-------|----|
| keydown → ws.send | ~50 |
| ws.send → server receive | ~2000 |
| PtyService.write → bash fd | ~50 |
| bash → fork subprocess | ~5000 |
| subprocess stdout → master fd | per-output |
| read_loop → subscriber queue | ~50 |
| subscriber queue → ws.send_bytes | ~50 |
| Total (excluding subprocess execution) | ~2.2 ms |
