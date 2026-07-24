# Phase 3 — Architecture Diagram & Process Topology

## Container Internals

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  Hugging Face Space Container  (debian:bookworm-slim)                      │
│                                                                             │
│  /entrypoint.sh  (PID 1)                                                    │
│  │                                                                          │
│  ├─► (existing prep) /data dirs · git clone · cleaner.py → SQLite heal        │
│  │                                                                          │
│  ├─► opencode serve --port 4096 --hostname 127.0.0.1     (PID 3)            │
│  │       │                                                                   │
│  │       └─► binds 0.0.0.0:4096  · chat UI · SSE · API · SQLite              │
│  │                                                                          │
│  └─► exec uvicorn backend.app.main:app --port 7860          (PID 4)         │
│          │                                                                   │
│          ├─► /bin/bash (slave pty, PID 5)                                   │
│          │       │                                                           │
│          │       └─► any child processes the user starts (bash forks)        │
│          │                                                                   │
│          ├─► HTTP handlers                                                  │
│          │       ├─► /                 → webapp (VSCode-style UI)           │
│          │       ├─► /health          → pty_alive + opencode status         │
│          │       ├─► /global/*, /api/*, /server/*, /assets/*, …           │
│          │       │       └─► reverse-proxy to opencode serve :4096          │
│          │       │                                                           │
│          │       └─► /terminal/snapshot → last 16 KB of PTY scrollback     │
│          │                                                                   │
│          └─► WS handlers                                                    │
│                  ├─► /terminal/ws   ←→  Python PtyService                   │
│                  └─► /*              ←→  opencode serve (events, etc.)      │
│                                                                             │
│  Network exposure:                                                           │
│      HF forwards external :7860  →  port-7860 inside the container.          │
│      :4096 is NOT exposed.                                                   │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Request / Data Flow

### Terminal request (browser → bash → browser)

```
                 ╭─────────────── Python PTY Service ───────────────╮
                 │                                                   │
  Browser        │                                                   │
  xterm.js   ────┼──► WebSocket  /terminal/ws  ──► PtyService.write  │
                 │       ▲                              │           │
                 │       │                              ▼           │
                 │       │                       os.write(master_fd)│
                 │       │                              │           │
                 │       │                              ▼           │
                 │       │                       /bin/bash (slave) │
                 │       │                              │           │
                 │       │                              ▼ stdout    │
                 │       │                       master_fd         │
                 │       │                              │           │
                 │       │      PtyService._read_loop ◄─┘           │
                 │       │              │                           │
                 │       │              ▼                           │
                 │       │       subscriber Queue (asyncio)         │
                 │       │              │                           │
                 │       │              ▼                           │
                 │       └───── ws.send_bytes(b"raw bytes")         │
                 │                                                  │
                 ╰──────────────────────────────────────────────────╯
```

### Chat request (browser → opencode → browser)

```
  Browser  ──►  GET /server/<base>/session/<id>
                       │
                       ▼                       FastAPI backward proxy
                _opencode_proxy(...)        ◄── (auth, headers, streams SSE)
                       │
                       ▼
                opencode serve :4096        ◄── actual chat + SSE streaming
                       │
                       ▼
                StreamingResponse (text/event-stream)
                       │
                       ▼
                Browser iframe (chat tab)
```

### Authenticated Streaming SSE

```
  chat SPA ──► GET /events (with Authorization: Basic …)
                   │
                   ▼
             _opencode_proxy: detects text/event-stream
                   │           or transfer-encoding: chunked
                   ▼
             StreamingResponse(aiter_bytes())
                   │
                   ▼
             Browser EventSource modal
```

## Lifecycles

### PTY Lifecycle

```
spawn ─► healthy ─► reads stream ─► broadcasts to subscribers
                  ▲                       │
                  │                       ▼
                  └───── resize ◄─── write to master fd
                                          │
                                          ▼
                                       exit (Ctrl-D, EOF, disconnect)
                                          │
                                          ▼
                                       cleanup: SIGTERM group, close fd
```

### WebSocket Lifecycle

```
client connect ─► server sends scrollback snapshot
                    │
                    ▼
                 client resize → server ioctl TIOCSWINSZ
                    │
                    ▼
                 two pumps (c2s + s2c), bounded by asyncio.Queue
                    │
                    ▼
                client disconnect  ─►  asyncio.CancelledError
                                       pty subscriber removed
                  (server PTY stays alive for next client)
```

## Component Sizes

| Component         | Lines of code | Source |
|-------------------|---------------|--------|
| `pty_service.py`  | ~210          | new, stdlib only |
| `main.py`         | ~220          | new |
| `opencode_proxy.py` | ~50          | new |
| `webapp.py`       | ~330          | new (HTML/CSS/JS) |
| `entrypoint.sh`   | ~115          | modified |
| `Dockerfile`      | ~60           | modified |
