# Phase 2 — Architecture Diagram & Relationship Analysis

## Repository Relationship Map

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        HUGGING FACE SPACES (3 repos)                         │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌──────────────────────┐    ┌──────────────────────┐    ┌───────────────┐  │
│  │   Opencode-Cli       │    │   OpenCode-Serve     │    │   MyHermes    │  │
│  │   (upstream server)  │    │   (bridge + webapp)  │    │   (unrelated) │  │
│  ├──────────────────────┤    ├──────────────────────┤    ├───────────────┤  │
│  │ Port: 4096           │    │ Port: 7860           │    │ Port: ?       │  │
│  │ opencode serve       │◄───│ Reverse proxy        │    │ Hermes CLI    │  │
│  │ (Go binary)          │    │ /proxy/{ws_id}/*     │    │ tui_gateway   │  │
│  │                      │    │                      │    │ acp_adapter   │  │
│  │ XDG dirs → /data     │    │ tmux sessions/user   │    │ hermes_app_api│  │
│  │ cleaner.py (DB fix)  │    │ workspace manager    │    │               │  │
│  │                      │    │ Telegram bot         │    │               │  │
│  │ NO terminal/PTY/WS   │    │ Webapp (HTML+WS)     │    │ NO terminal   │  │
│  │ NO chat UI           │    │                      │    │ for OpenCode  │  │
│  └──────────────────────┘    └──────────────────────┘    └───────────────┘  │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Current OpenCode‑Serve Runtime Architecture

```
                    ┌────────────────────────────────────────────────────┐
                    │           Single Container (Python 3.12)            │
                    │                  Port 7860 (HF)                     │
                    └────────────────────────────────────────────────────┘
                                      │
                    ┌─────────────────┼─────────────────┐
                    ▼                 ▼                 ▼
            ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
            │   uvicorn    │  │ opencode     │  │   tmux       │
            │  (FastAPI)   │  │ serve (4096) │  │  sessions    │
            │  main.py     │  │ (Go binary)  │  │  /user_{id}  │
            └──────┬───────┘  └──────┬───────┘  └──────┬───────┘
                   │                 │                 │
         ┌─────────┼─────────┐       │       ┌────────┼────────┐
         ▼         ▼         ▼       ▼       ▼        ▼        ▼
    /api/*    /proxy/*    /ws/*  WebSocket   pipe-pane  capture
    /api/     /ws/{id}/*  rev-proxy /api/   → .log     -pane
    ws/       reverse    to 4096  ws/session
    session/  proxy
```

## Target Architecture (After Integration)

```
                    ┌────────────────────────────────────────────────────┐
                    │           Single Container (Python 3.12)            │
                    │                  Port 7860 (HF)                     │
                    └────────────────────────────────────────────────────┘
                                      │
          ┌───────────────────────────┼───────────────────────────┐
          ▼                           ▼                           ▼
  ┌──────────────┐            ┌──────────────┐            ┌──────────────┐
  │   uvicorn    │            │ opencode     │            │   ttyd       │
  │  (FastAPI)   │            │ serve (4096) │            │  (7681)      │
  │  main.py     │            │ (Go binary)  │            │  xterm.js    │
  └──────┬───────┘            └──────┬───────┘            └──────┬───────┘
         │                           │                           │
  ┌──────┼──────┐            ┌───────┼───────┐            ┌──────┼──────┐
  ▼      ▼      ▼            ▼       ▼       ▼            ▼      ▼      ▼
/api/  /ws/  /terminal*   4096   4100-   tmux       7681   /term   /term/ws
/ws/   session/  route*       4999   sessions          route  proxy
```

*New: `/terminal` static page (xterm.js) + `/terminal/ws` reverse-proxy to `ttyd` on 7681.

## Communication Flow (Post-Integration)

```
┌──────────────┐     HTTPS/WS      ┌────────────────────────────────────┐
│   Browser    │ ◄──────────────► │  OpenCode-Serve (7860)              │
│              │                   │  ├─ /api/*        → opencode 4096  │
│  ┌────────┐  │                   │  ├─ /ws/*         → opencode 4096  │
│  │ WebApp │  │                   │  ├─ /terminal     → static HTML   │
│  │ (chat) │  │                   │  └─ /terminal/ws  → ttyd 7681     │
│  └────────┘  │                   │                                     │
│  ┌────────┐  │                   │  ┌──────────────────────────────┐  │
│  │TermTab │──┼───────────────────┼─►│  ttyd (7681)                  │  │
│  │(xterm) │  │  wss://...        │  │  └─ bash login shell          │  │
│  └────────┘  │                   │  │     Same /data, $HOME, env    │  │
└──────────────┘                   │  │     Same installed packages   │  │
                                   │  └──────────────────────────────┘  │
                                   └────────────────────────────────────┘
```

## File-to-Component Mapping

| Component | Source File(s) |
|-----------|----------------|
| FastAPI app + lifespan | `backend/app/main.py` |
| HTTP reverse proxy | `backend/app/main.py:185-310` |
| WebSocket reverse proxy (opencode) | `backend/app/main.py:102-183` |
| WebSocket session API (tmux) | `backend/app/api.py:340-385` |
| tmux SessionManager | `core/session_manager.py` |
| WorkspaceProcessManager | `services/workspace_process_manager.py` |
| ProcessRegistry | `services/process_registry.py` |
| Telegram bot | `bot/telegram_bot.py` |
| Webapp HTML | `backend/app/webapp_html.py` |
| Entrypoint (process boot) | `scripts/entrypoint.sh` |
| Dockerfile | `Dockerfile` |

## Decision: Experimental Playground = OpenCode‑Serve

**Reasoning:**
1. Only repo that already contains terminal/PTY/shell/WS implementation
2. Already runs on HF Spaces at port 7860 (webapp port)
3. Has Dockerfile, entrypoint, workspace persistence, auth, Telegram
4. Adding `ttyd` + `/terminal` route is a natural enrichment, not a reconstruction
5. Opencode‑Cli must remain untouched (per brief) — it's the upstream AI server
6. MyHermes is a different product with no reusable terminal for OpenCode