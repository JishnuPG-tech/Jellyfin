# OpenCode-Serve Embedded Terminal — Architecture Report

## 1. Current State (As Cloned From OpenCode-CLI)

```
┌─────────────────────────────────────────────────────────────────────┐
│  Hugging Face Space Container                                       │
│  ─────────────────────────────                                       │
│  Base image: debian:bookworm-slim + curl + git + python3           │
│                                                                      │
│  /entrypoint.sh                                                      │
│      │                                                               │
│      ├─► mkdir /data/{share,config,cache,state}/opencode             │
│      ├─► python3 /cleaner.py &  (SQLite self-heal daemon)            │
│      ├─► cd /projects/default                                        │
│      ├─► git clone JishnuPG-tech/OpenCode-Drive.git (or pull)        │
│      └─► exec opencode serve --port 4096 --hostname 0.0.0.0          │
│                                                                      │
│  Listens on: :4096                                                   │
│                                                                      │
│  /data                                  (HF persistent dataset mount)│
│  ├─ share/opencode/opencode.db                                         │
│  ├─ config/opencode/opencode.json  (model: opencode/big-pickle)      │
│  └─ cache/state/                                                       │
└─────────────────────────────────────────────────────────────────────┘
```

### Current Execution Pipeline (From The Opencode Binary)

Because the opencode binary is a Go binary, we cannot see its internals.
We **infer** the pipeline from observable behavior at `/global/health`,
`/server/.../session/...`, and `/find/file`:

| Step | Observable Evidence |
|------|-----|
| 1. AI generates a command from a tool call | Opencode writes to `/data/share/opencode/opencode.db` (SQLite sessions) |
| 2. Tool call parsed & dispatched internally | `/api/*` endpoints |
| 3. Shell command executed | Likely `subprocess` or `pty` inside the Go binary (we do not know exact path) |
| 4. stdout/stderr collected | Streamed via SSE to the chat UI's `/server/<base>/session/<id>` page |
| 5. Working directory managed | `opencode.db` `session.directory` column (we saw this in `cleaner.py`) |
| 6. Env vars inherited | `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` from entrypoint, persisted into config |
| 7. Process cleanup | Performed by `opencode` binary itself |
| 8. Cancellation | Ctrl-C forwarded via the chat UI (most likely) |

**Conclusion**: We cannot change opencode's internal execution path. The only
realistic way to get a "shared" terminal is to:
1. Run a persistent `bash` PTY inside the container.
2. Allow the user to invoke `opencode` (the interactive CLI) **inside** that PTY.
3. Keep `opencode serve` running for the HTTP chat UI.
4. The webapp shows BOTH: the live terminal and a proxied iframe/redirect of the chat UI.

## 2. What The New Architecture Must Provide

```
┌─────────────────────────────────────────────────────────────────────┐
│  NEW OpenCode-Serve Container                                        │
│                                                                      │
│  /entrypoint.sh (modified)                                           │
│      │                                                               │
│      ├─► (existing prep) /data dirs, git clone, cleaner.py            │
│      │                                                               │
│      ├─► opencode serve --port 4096 --hostname 127.0.0.1 &            │
│      │                                                               │
│      ├─► exec uvicorn backend.app.main:app --port 7860                │
│      │       │                                                       │
│      │       └─► FastAPI app (python: built-in + pendulum deps)       │
│      │              ├─► Python PTY service (1 bash PTY, persistent)  │
│      │              ├─► HTTP proxy to opencode serve on :4096         │
│      │              ├─► WebSocket proxy to opencode serve on :4096    │
│      │              └─► Static webapp at `/` (xterm.js + chat tab)   │
│      │                                                               │
│      └─► Listens on :7860 (HF `app_port: 7860`)                      │
│                                                                      │
│  /data                                  (HF persistent dataset mount)│
│  ├─ share/opencode/opencode.db   ← opencode sessions (unchanged)     │
│  ├─ config/opencode/opencode.json                                       │
│  ├─ workspaces/{default,user_*}  ← for terminal cwd                   │
│  └─ logs/{opencode-serve.log, uvicorn.log, pty.log, ttyd.log}        │
└─────────────────────────────────────────────────────────────────────┘
```

### New Execution Pipeline

#### 2.1 Pure Terminal Path

```
xterm.js in browser
   │ WebSocket {binary, text} ◄──► /terminal/ws
   ▼
FastAPI WebSocket endpoint
   │
   ▼
Python PTY Process
   │ stdin = bytes from browser key input
   │ stdout = bytes from /bin/bash → forward to browse
   ▼
/bin/bash  (a real PTY)
   ├── pwd → working directory (/data/workspaces/default)
   ├── any command → captures stdout/stderr
   └── exit code → reported on next prompt or via marker
```

#### 2.2 Chat-Only Path (Preserved)

```
Proxied iframe/link in tab
   │
   ▼ GET /server/<base>/session/<id>
FastAPI /{path:path} catch-all proxy
   │
   │ Authorization: Basic ${OPENCODE_SERVER_USERNAME}:${PASSWORD}
   ▼
opencode serve @ :4096
   │
   ▼
SPA HTML or JSON/SSE
```

Both paths live **in the same container, same Python process, same
filesystem, same env vars**.

## 3. Why A Python PTY (Not ttyd)

| Concern | Python PTY | ttyd |
|--------|-----------|------|
| Image size | 0 extra bytes (stdlib) | +2 MB binary download |
| Reconnect | Reattach FDs to same PID | New bash per reconnect |
| Resize | Direct `ioctl(TIOCSWINSZ)` via fcntl | CLI flag at start |
| Multi-attach | One persistent PID, broadcast | Per-ttyd-instance session |
| History | bash saves `.bash_history` for the live shell | Same |
| Maintenance | All Python, no second protocol | ttyd has its own /ws protocol |
| Backpressure | asyncio queue between reader loop & WS writer | Native ttyd buffer |
| Session kill / cleanup | `os.killpg` to group | `ttyd-signal` SIGTERM |

**Decision**: Implement our own PTY service in Python using
`pty.openpty()` + `os.fork()`/`subprocess.Popen` + asyncio. Saves 2 MB,
removes a dependency, gives us precise control over resize, reconnect,
and graceful shutdown.

## 4. File Layout

```
opencode-serve/
├── Dockerfile                  (modified — add Python pip deps)
├── docker-compose.yml          (changed `app_port` to 7860)
├── entrypoint.sh               (modified — bootstrap uvicorn instead of bare opencode)
├── README.md                   (updated — terminal features)
├── cleaner.py                  (unchanged from opencode-cli)
├── requirements.txt            (new — fastapi, uvicorn, httpx, websockets, pexpect, ptyprocess)
├── backend/
│   └── app/
│       ├── __init__.py
│       ├── main.py             (FastAPI app — wsgateway + reverse proxy + webapp)
│       ├── pty_service.py      (NEW — single persistent PTY + asyncio broadcaster)
│       ├── opencode_proxy.py   (NEW — HTTP/WS reverse proxy helpers)
│       └── webapp.html         (NEW — xterm.js + dark theme + chat/term switcher)
└── docs/                       (NEW — architecture + operations docs)
```

## 5. Process Topology

```
┌──────────────────────────────────────────────────────────────┐
│  HF Space Container (one cgroup)                              │
│                                                              │
│   PID 1 = entrypoint.sh (background)                          │
│   ├── PID 2 = python3 /cleaner.py                            │
│   ├── PID 3 = opencode serve (port 4096)                      │
│   ├── PID 4 = uvicorn backend.app.main:app (port 7860)         │
│   │       ├── PID 5 = /bin/bash (PTY master+slaver)  ◄────── the shared terminal
│   │       └── PID 6+ = any subprocesses started by bash      │
│   │               (git, pip, npm, pnpm, python, etc.)         │
│   │                                                            │
│   └── (cleanup on SIGTERM: uvicorn → pty.cleanup() → SIGKILL bash) │
└──────────────────────────────────────────────────────────────┘
```

## 6. Critical Design Decisions

1. **Single shared PTY per space** (not per-user). Hugging Face spaces are
   inherently single-user — one PTY is enough and avoids multi-tenancy complexity.
2. **Bash as `/bin/bash -l` (login shell)**. Sources `/etc/profile`, `/data/.profile`
   so the terminal has the same env as the rest of the container, including
   the opencode binary on PATH.
3. **Browser-side rendering (xterm.js), server-side PTY (Python)**.
   This is the proven VS Code / Cursor / Hyper pattern.
4. **WebSocket for stream I/O** (not SSE). SSE is one-way; we need full
   bidirectional I/O with proper backpressure. (See Phase 7 for full
   analysis.)
5. **Container reuse**: We do NOT spin up a second container. The PTY runs
   inside the same uvicorn worker.

## 7. Open Questions / Risks

- **opencode's internal command execution does NOT pipe through our PTY.**
  We deliver "shared terminal" semantics for the *interactive opencode CLI*
  (the user runs `opencode` inside our PTY) and a chat UI in a side panel.
  This is the best achievable without modifying the Go binary.
- **Multiple browser tabs**: Two browser tabs opening the same PTY will see
  the same output (broadcast) and both can type, which can collide. We do
  not attempt to serialize — HS spaces are single-user anyway.
