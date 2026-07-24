# Phase 3e — Conflict Resolution Plan

## Docker Conflicts
| Conflict | Status | Resolution |
|----------|--------|------------|
| Base image | `python:3.12-slim` vs `debian:bookworm-slim` (Opencode-Cli) | **No conflict** — OpenCode-Serve stays on python:3.12-slim; Opencode-Cli untouched |
| Port exposure | 7860 (HF) vs 4096 (opencode) vs 7681 (ttyd) | **Resolved** — only 7860 exposed; 4096 & 7681 bound to 127.0.0.1 |
| Volume mounts | `/data/workspaces`, `/data/logs` vs `/data` (Opencode-Cli) | **No conflict** — different Spaces, different mounts |
| Entrypoint | Single bash script vs tini/supervisor | **Kept** bash script; added ttyd before uvicorn `exec` |

## Startup Conflicts
| Conflict | Status | Resolution |
|----------|--------|------------|
| Process supervision | opencode serve (bg) + uvicorn (fg) | Added ttyd as 3rd bg process before `exec uvicorn` |
| Healthcheck | HF hits 7860; opencode serves 4096 | Entrypoint waits for 4096 before starting uvicorn (existing logic kept) |
| ttyd readiness | No health endpoint | Not needed — browser handles reconnection; ttyd starts in <1s |

## Routing Conflicts
| Path | Current Owner | New Owner | Resolution |
|------|---------------|-----------|------------|
| `/` | Catch-all proxy to 4096 | Keep | Terminal at `/terminal` only |
| `/ws/...` | opencode WS proxy | Keep | New `/terminal/ws` is separate |
| `/api/...` | REST API | Keep | Unchanged |
| `/terminal` | **NEW** | Terminal HTML | No collision |
| `/terminal/ws` | **NEW** | ttyd WS proxy | No collision |

## WebSocket Conflicts
| Aspect | Existing | New | Resolution |
|--------|----------|-----|------------|
| Upgrade handling | `main.py` catches `upgrade` header | Same | FastAPI routes by path first; `/terminal/ws` wins before catch-all |
| Auth | Basic auth header injected | None (same-origin) | Browser sends cookies/origin; no new auth surface |

## Authentication Conflicts
| Mechanism | Scope | Terminal Impact |
|-----------|-------|-----------------|
| HTTP Basic (`OPENCODE_SERVER_*`) | opencode API proxy | Not needed for `/terminal` (same-origin browser tab) |
| Telegram `AUTHORIZED_USERS` | Bot commands | Terminal tab not exposed to Telegram |

## Environment Variable Conflicts
| Var | Used By | Terminal Uses | Conflict? |
|-----|---------|---------------|-----------|
| `WORKSPACE_PATH` | entrypoint, workspace mgr | ttyd inherits cwd | No |
| `HOME=/data` | entrypoint | ttyd inherits | No |
| `OPENCODE_SERVER_*` | proxy auth | N/A | No |
| `BOT_TOKEN` | Telegram | N/A | No |
| `TTYD_PORT` | **NEW** | entrypoint | Documented in `.env.example` |

## Package Conflicts
**None.** Zero Python packages added. `ttyd` is a static binary.

## Docker Build Conflicts
| Step | Potential Issue | Mitigation |
|------|----------------|------------|
| `apt-get install` + ttyd curl | Layer caching | Combined in single `RUN` with existing apt line |
| `pip install -r requirements.txt` | Unchanged | No change |
| `COPY . /app` | Unchanged | No change |

## HF Spaces Deployment Conflicts
| Config | Current | After | Notes |
|--------|---------|-------|-------|
| `app_port` | 7860 | 7860 | Unchanged |
| `sdk: docker` | ✓ | ✓ | Unchanged |
| Healthcheck | None | None | HF uses port 7860 TCP |
| Secrets | `BOT_TOKEN`, `AUTHORIZED_USERS`, `OPENCODE_SERVER_*`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | **Same** | No new secrets |
| Persistent storage | `/data` dataset | **Same** | Unchanged |

## Rollback Plan
If terminal integration breaks anything:
1. `git revert` the 3-4 commits on `feat/embedded-terminal`
2. Docker rebuild uses original `Dockerfile` + `entrypoint.sh`
3. Zero data migration — `/data` layout identical
4. HF Space redeploys in ~2 minutes