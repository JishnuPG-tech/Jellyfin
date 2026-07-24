# Deployment Notes

## Hugging Face Space requirements (Docker SDK)

* Single Dockerfile at the repo root.
* Expose exactly one port (`app_port` in README YAML).
* Default port 7860 (`0.0.0.0:7860`). Anything else breaks HF's load balancer.
* ENTRYPOINT must exec the long-lived process (`uvicorn`), not just a shell,
  so that SIGTERM on container shutdown reaches the worker.

We respect all of these.

## Persistent storage

The HF Space `Jishnupg/Opencode-Cli-storage` dataset is mounted at `/data`.
We use it for:

| Path                       | Used for                                    |
|----------------------------|---------------------------------------------|
| `/data/share/opencode/`    | `opencode.db` (sessions + messages)         |
| `/data/config/opencode/`   | `opencode.json` (model, server config)      |
| `/data/cache/opencode/`    | opencode download cache                      |
| `/data/state/opencode/`    | opencode internal state                      |
| `/data/workspaces/default` | Terminal cwd                                 |
| `/data/logs/*.log`         | Diagnostic logs (`opencode-serve.log`, `cleaner.log`, etc.) |

## Env vars

| Var | Default | Purpose |
|-----|---------|---------|
| `PORT` | `7860` | What FastAPI listens on **and** what HF proxies. |
| `ANTHROPIC_API_KEY` | —  | Model key. |
| `OPENAI_API_KEY`    | —  | Model key. |
| `OPENCODE_SERVER_USERNAME` | `opencode` | HTTP-Basic auth on opencode. |
| `OPENCODE_SERVER_PASSWORD` | `password` | HTTP-Basic auth on opencode. |
| `GITHUB_REPO`          | `JishnuPG-tech/OpenCode-Drive.git` | Repo cloned into `/projects/default`. |
| `WORKDIR`              | `/data/workspaces/default` | Terminal cwd. |
| `LOG_LEVEL`            | `INFO`    | uvicorn log level. |

## Secrets

In production, set `OPENCODE_SERVER_USERNAME` / `OPENCODE_SERVER_PASSWORD`
on the Space (Settings → Repository secrets) to lock down the opencode proxy.

## First boot vs subsequent boots

* **First boot**: `/data` is empty. `entrypoint.sh` initializes everything
  (config, db, project clone, PTY spawn).
* **Subsequent boots**: the same code runs but the data persists.

## Logs

| File | What |
|------|------|
| `/data/logs/opencode-serve.log` | upstream opencode binary stdout |
| entrypoint stdout is captured by HF and shown under "Logs". |

## Reverse-proxy and WebSocket tunnelling

HF forwards TCP for plain HTTP and WebSocket. Both are fine —
`uvicorn[standard]` and `uvicorn[websockets]` handle the upgrade.
