---
title: OpenCode Serve
emoji: 🖥️
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# OpenCode-Serve

Production-ready OpenCode AI serving environment with an **embedded live
terminal**.

* **Webapp**: `https://<your-space>.hf.space/` — VSCode-style integrated UI
  with a `Terminal` tab and a `Chat` tab.
* **Embedded terminal** (`/bin/bash -i` run in the same container as the
  OpenCode server) speaks WebSocket at `/terminal/ws`.
* **Upstream chat**: the standard `opencode serve` SPA stays reachable; we
  proxy `/server/<base>/session/<id>` straight to `:4096`.
* **Persistent data**: `/data` bucket (sessions, configs, terminal scrollback).

## Quick links

| Path | What it serves |
|------|----------------|
| `/`                           | Integrated webapp (terminal + chat tabs) |
| `/terminal/ws`                | WebSocket endpoint for xterm.js terminal |
| `/health`                     | Service health (PTY + opencode status)    |
| `/global/health`              | Upstream opencode health                  |
| `/server/<base>/session/<id>` | Proxied opencode chat SPA                 |
| `/api/*`, `/assets/*`, `/v1/*` | Proxied opencode endpoints              |

## Connecting the mobile app (legacy compatibility)

In the mobile client settings, enter:
```
https://<your-space>.hf.space
```
Then choose an **auth type** if you have set `OPENCODE_SERVER_USERNAME` /
`OPENCODE_SERVER_PASSWORD` as Space secrets.

## Endpoints summary

* `opencode serve` is bound to `127.0.0.1:4096` *inside* the container.
* `uvicorn` (the FastAPI gateway including the terminal) is bound to
  `0.0.0.0:7860`. The Hugging Face Space exposes that port.
* The PTY service is in-process (`backend/app/pty_service.py`); it does
  not consume an extra port.

## Why a Python PTY instead of `ttyd`?

A small `ttyd` binary is convenient, but `feat/embedded-terminal` adds a
real Python PTY service that:

* matches the OpenCode container's env perfectly (same cwd, env vars, PATH);
* reconnects with scrollback replay;
* handles `TIOCSWINSZ` resizing cleanly;
* costs ~120 lines of code and zero extra binary.

## Repository layout

```
opencode-serve/
├── backend/
│   └── app/
│       ├── main.py          # FastAPI app: routes + reverse proxy
│       ├── pty_service.py   # /bin/bash PTY service (persistent)
│       ├── opencode_proxy.py
│       └── webapp.py        # VSCode-style integrated UI
├── cleaner.py               # SQLite self-heal daemon (copied from opencode-cli)
├── entrypoint.sh            # OpenCode + uvicorn bootstrap
├── Dockerfile               # HF-Docker-Spaces-compatible image
├── docker-compose.yml
├── docs/
│   └── ARCHITECTURE.md
└── requirements.txt
```
