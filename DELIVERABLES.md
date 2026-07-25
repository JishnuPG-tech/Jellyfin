# OpenCode-Serve — Final Working State

## Architecture
```
HF Load Balancer (HTTP/2)
    ↓
uvicorn gateway :7860 (HF-exposed)
    ├─ /terminal/*  → ttyd :7681 (embedded bash)
    ├─ /            → redirect HTML (auto-load latest session)
    ├─ /server/<base> (bare) → redirect HTML
    └─ /*           → opencode serve :4096 (chat API + SPA)
```

## Key Files
- `Dockerfile` — debian:bookworm-slim + python3 + opencode + ttyd + uvicorn/httpx
- `entrypoint.sh` — starts ttyd, opencode serve, uvicorn gateway
- `backend/app/main.py` — FastAPI gateway with proxy + redirect logic
- `cleaner.py` — SQLite self-healing daemon (from opencode-cli)

## Routes Verified
| Route | Behavior | Status |
|-------|----------|--------|
| `GET /` | Redirect HTML → auto-load latest session | ✅ |
| `GET /server/<base>` | Redirect HTML → auto-load latest session | ✅ |
| `GET /server/<base>/session/<id>` | Chat UI (opencode SPA) | ✅ |
| `GET /api/session` | Proxy → opencode (50 sessions) | ✅ |
| `GET /global/health` | Proxy → opencode | ✅ |
| `GET /terminal/` | Proxy → ttyd (embedded bash) | ✅ |
| `GET /_healthz` | Gateway health check | ✅ |

## URLs
- **Space**: https://jishnupg-opencode-serve.hf.space
- **Chat**: `https://jishnupg-opencode-serve.hf.space/server/<base64(SPACE_URL)>/session/<session_id>`
- **Terminal**: https://jishnupg-opencode-serve.hf.space/terminal/
- **API**: https://jishnupg-opencode-serve.hf.space/api/session

## Key Learnings
1. **Previous proxy failures (HTTP/2 INTERNAL_ERROR)**: Caused by using `StreamingResponse` for ALL responses. Fixed by buffering normal responses and only streaming SSE/chunked.
2. **ttyd `--base-path`**: Doesn't work as expected. Fixed by stripping `/terminal` prefix in proxy.
3. **`PYTHONPATH=/app`**: Required because entrypoint `cd`s to `/projects/default`.
4. **HF only exposes one port (7860)**: Must proxy everything through a single gateway.
