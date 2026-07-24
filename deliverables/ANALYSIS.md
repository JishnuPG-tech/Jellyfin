# Phase 1 — Repository Analysis Report

## 1. Opencode‑Cli (`huggingface.co/spaces/Jishnupg/Opencode-Cli`)

| Field | Details |
|-------|---------|
| **Purpose** | Thin Docker host that runs `opencode serve` on port 4096 and persists all state to the HF `/data` dataset bucket. This is the **upstream server** for the OpenCode mobile client. |
| **Stack** | Debian bookworm-slim, `opencode` Go binary (v1.18.3), Python 3 for `cleaner.py` |
| **Dockerfile** | `/tmp/Opencode-Cli/Dockerfile:1` |
| **Entrypoint** | `/tmp/Opencode-Cli/entrypoint.sh:1` (155 lines, `sh`) |
| **Process supervision** | None. Single `exec opencode serve --port 4096 --hostname 0.0.0.0` at line 155. Background SQLite self-healing daemon (`cleaner.py`) only. |
| **HTTP server** | Provided by `opencode` binary — `/api/*`, `/global/health` |
| **WebSocket** | None added; whatever `opencode` serves internally |
| **SSE** | None |
| **Streaming logic** | None in this repo; handled by the `opencode` binary |
| **Authentication** | HTTP Basic via `OPENCODE_SERVER_USERNAME` / `OPENCODE_SERVER_PASSWORD` (`docker-compose.yml:13`) |
| **API endpoints** | No custom endpoints; only the binary's own |
| **Terminal/shell/PTY** | **None** |
| **Env vars** | `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENCODE_SERVER_USERNAME`, `OPENCODE_SERVER_PASSWORD`, `GITHUB_REPO` |
| **Persistent data** | `XDG_*_HOME=/data/{share,config,cache,state}` (`Dockerfile:5-8`) |
| **Default model** | `opencode/big-pickle` (`entrypoint.sh:49`) |
| **HF app_port** | `4096` |
| **Special** | `cleaner.py` walks `opencode.db` and fixes UTF-8 corruption in `session` and `project_directory` tables (`cleaner.py:19-83`) |

---

## 2. OpenCode‑Serve (`huggingface.co/spaces/Jishnupg/OpenCode-Serve`)

| Field | Details |
|-------|---------|
| **Purpose** | Production bridge on top of `opencode`. FastAPI server that the Android app uses. Provides tmux-backed shells, per-user workspaces, a Telegram bot, HTTP/WS reverse proxy to `opencode serve`, and a basic HTML5 webapp. **This is the only repo that already has a terminal/PTY/WS/shell implementation.** |
| **Stack** | Python 3.12, FastAPI, uvicorn, tmux, pexpect, python-telegram-bot, httpx, websockets, SQLAlchemy<2, aiosqlite, redis, supervisor |
| **Dockerfile** | `/tmp/opencode-serve/Dockerfile:1` |
| **Entrypoint** | `/tmp/opencode-serve/scripts/entrypoint.sh:1` (85 lines, bash) — boots `opencode serve` on 4096, waits up to 45s for `/global/health`, then runs uvicorn on 7860 |
| **HF app_port** | `7860` |
| **Healthcheck** | None in compose; HF uses port 7860 HTTP |
| **Persistence** | `/data/workspaces`, `/data/workspaces.json`, `/data/logs/*.log` |
| **Default workspace prep** | `entrypoint.sh:33-46` creates `projects/`, `my-code/`, `scratch/` with README stubs |
| **Process supervision** | None — both `opencode serve` (background) and uvicorn run from the same entrypoint script. `opencode serve` dies if entrypoint is restarted (no respawn). |
| **App entry** | `uvicorn backend.app.main:app` (`entrypoint.sh:85`) |
| **Custom API** | `/api/sessions/*`, `/api/workspace/*`, `/api/debug/*`, `/api/telegram-webhook` (`api.py`) |
| **Reverse proxy** | `/proxy/{workspace_id}/{path:path}` and catch-all `/{path:path}`; `find/file` intercepted locally (`main.py:185-310`) |
| **WebSocket** | Two layers:<br>• `ws://.../{path:path}` (legacy single-user) and `ws://.../ws/{workspace_id}/{path:path}` (per-workspace) reverse-proxy into `opencode` (`main.py:102-183`)<br>• `/api/ws/session/{user_id}` (`api.py:340-385`) consumes tmux session output via `SessionManager.stream_output` |
| **Session manager** | `core/session_manager.py:1` — `tmux new-session -d -s opencode_user_<id> -c <ws> bash`, `tmux pipe-pane` to `<ws>/.opencode_output.log`, `tmux send-keys`, `send C-c` for interrupt, `tmux capture-pane -pS -N` for last-N-lines |
| **Workspace manager** | `services/workspace_process_manager.py:1` — spawns fresh `opencode serve` per workspace on dynamic port 4100–4999, idle shutdown after 30 min |
| **Process registry** | `services/process_registry.py:1` — JSON registry `/data/workspaces.json` |
| **Higher-level wrapper** | `services/opencode_manager.py:1` |
| **Telegram** | `bot/telegram_bot.py:1` (724 lines) — raw polling mode AND webhook mode (`run_bot_async` decides; `HF_URL` env → webhook only). Webhook registration needs manual `setWebhook` because HF has no outbound HTTP (lines 643-653) |
| **Webapp HTML** | `backend/app/webapp_html.py:1` (467 lines) — vanilla JS, polls `/api/workspace/list` and `/api/workspace/status`, connects to `ws://.../api/ws/session/<user_id>`, sends `{type:"command",text:...}`, `{type:"key",key:...}`, `{type:"interrupt"}`. Strips ANSI. Renders Google OAuth cards |
| **Authentication** | HTTP Basic for opencode (`OPENCODE_SERVER_USERNAME`/`_PASSWORD`, base64 in `main.py:61-64`); Telegram whitelist via `AUTHORIZED_USERS` env (csv of user IDs) |
| **Streaming** | tmux `pipe-pane` writes `.opencode_output.log`; `SessionManager.stream_output` tails the file and yields chunks; `api.py:336-385` ships them to the WebSocket as JSON `{type:"output",text:...,is_card:...}` |
| **Env vars** | `BOT_TOKEN`, `AUTHORIZED_USERS`, `WORKSPACE_PATH`, `UPLOAD_LIMIT`, `TELEGRAM_BASE_URL`, `HF_URL`, `OPENCODE_SERVER_USERNAME`, `OPENCODE_SERVER_PASSWORD`, `LOG_LEVEL`, plus everything `opencode` itself reads |
| **Dependencies** | `requirements.txt:1` — fastapi, uvicorn[standard], python-telegram-bot[aio], httpx, pexpect, tmuxp, aiosqlite, SQLAlchemy<2, python-dotenv, aiofiles, pytest, psutil, redis, python-multipart, pydantic, gunicorn, supervisor, typing-extensions |

---

## 3. MyHermes (`huggingface.co/spaces/Jishnupg/MyHermes`)

| Field | Details |
|-------|---------|
| **Purpose** | Completely separate Python monorepo (~37k lines for `cli.py` alone) for the Hermes/Nous AI product. Unrelated to OpenCode. |
| **Stack** | Python, curses TUI, JSON-RPC over stdio, separate gateway, ACP adapter, `hermes_app_api.py` (OpenAI-compatible FastAPI) |
| **Relationship** | Only link: `android-terminal/README.md` references `https://jishnupg-myhermes.hf.space` as an externally-hosted ttyd/tmux terminal URL. The MyHermes source tree itself contains no reusable live terminal for OpenCode. |
| **Verdict** | Out of scope for this integration. |

---

## Key Finding

**Opencode‑Cli has no terminal, no chat UI, no SSE/WS, no shell, no PTY.** It only runs the upstream `opencode` binary. The brief's mental model ("merge the terminal from Opencode‑Cli") is inverted — the terminal/shell/PTY/WebSocket implementation already lives **entirely inside OpenCode‑Serve**. The correct integration direction is: keep Opencode‑Cli untouched on HF (upstream AI server), and add a true browser-side live terminal (`ttyd` + xterm.js) **inside the OpenCode‑Serve container** so the webapp can surface it at `/terminal`.