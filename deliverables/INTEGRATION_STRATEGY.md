# Phase 3 — Integration Strategy

## Goal
Add a **production-quality embedded terminal** (xterm.js + ttyd) to the OpenCode‑Serve webapp at `/terminal`, sharing the same container, filesystem, shell, working directory, environment, packages, and bash session that the AI agent uses. The terminal must:
- Stream live output (build logs, git, npm, pip, apt, python, node, shell commands)
- Accept manual user input
- Share the tmux session with the AI agent when practical
- Not break any existing feature (chat UI, SSE, WS, Telegram, auth, workspaces, Docker)

## High-Level Approach
1. **Add `ttyd`** to the Docker image (port 7681, internal only)
2. **Add `/terminal` static page** serving xterm.js that connects to `/terminal/ws`
3. **Add reverse-proxy route** `/terminal/ws` → `ws://127.0.0.1:7681` in FastAPI
4. **Boot `ttyd` from entrypoint** (same shell, same `$HOME`, same workspaces)
5. **Add a "Terminal" tab/button** to the existing webapp topbar
6. **Optional: share tmux session** — `ttyd tmux attach -t opencode_user_<id>` so AI + user see identical panes

## Source-of-Truth: What to Copy / Merge / Keep

| From Repo | Files | Action | Target Location |
|-----------|-------|--------|-----------------|
| **Opencode‑Cli** | `Dockerfile`, `entrypoint.sh`, `cleaner.py`, `docker-compose.yml`, `.env.example` | **DO NOT TOUCH** (per brief). Reference only for model/env defaults. | N/A |
| **OpenCode‑Serve (base)** | Everything | **Keep as base** — this IS the experimental repo | `/tmp/opencode-serve/` (working copy) |
| **MyHermes** | None | **Ignore** — no reusable terminal for OpenCode | N/A |
| **New (this integration)** | `ttyd` install, xterm.js bundle, `/terminal` route, entrypoint additions, webapp tab | **Add fresh** | See file-by-file plan below |

## File-by-File Migration Plan

### 1. Dockerfile — add `ttyd` + static assets
**File:** `Dockerfile`
```dockerfile
# After line 18 (after apt-get install ...), add:
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
  && curl -fsSL "https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.x86_64" \
       -o /usr/local/bin/ttyd \
  && chmod +x /usr/local/bin/ttyd \
  && rm -rf /var/lib/apt/lists/*

# Add xterm.js bundle for /terminal static page (or serve via CDN)
# Option A: bundle locally — copy web/terminal/ to /app/web/terminal/
# Option B: use CDN in HTML — simpler, zero build step
```

### 2. Entrypoint — boot `ttyd` alongside existing services
**File:** `scripts/entrypoint.sh`
```bash
# After line 50 (opencode serve started) and before line 84 (uvicorn start):
# Start ttyd on port 7681, binding to 127.0.0.1, with the SAME shell/env as the user
export HOME="/data"   # already set at line 20
cd "${WORKSPACE_PATH}" || cd /data/workspaces

# ttyd with: login shell, same working dir, same env, reconnect support
ttyd -p 7681 -i 127.0.0.1 \
  --writable \
  --title "OpenCode Terminal" \
  --signal 1 \
  --client-option '{"fontSize":13,"fontFamily":"JetBrains Mono, monospace"}' \
  bash -l > /data/logs/ttyd.log 2>&1 &
TTYD_PID=$!
echo "ttyd started (PID: $TTYD_PID) on port 7681"

# Optional: if we want shared tmux session with AI agent:
# ttyd -p 7681 -i 127.0.0.1 --writable tmux attach -t opencode_user_<id>
# But <id> is per-user; needs dynamic route. Phase 1: static bash login.
```

### 3. FastAPI — add `/terminal` routes
**File:** `backend/app/main.py`

Add after existing websocket proxy (around line 183):
```python
# ── Terminal (ttyd) reverse proxy ──────────────────────────────────────
@app.websocket("/terminal/ws")
async def terminal_ws_proxy(client_ws: WebSocket):
    """Proxy browser xterm.js WebSocket to ttyd on 7681."""
    await client_ws.accept()
    uri = "ws://127.0.0.1:7681"
    if client_ws.query_params:
        uri += f"?{client_ws.query_params}"
    try:
        async with websockets.connect(uri) as server_ws:
            async def c2s():
                try:
                    while True:
                        msg = await client_ws.receive()
                        if "text" in msg:
                            await server_ws.send(msg["text"])
                        elif "bytes" in msg:
                            await server_ws.send(msg["bytes"])
                except Exception:
                    pass
            async def s2c():
                try:
                    async for msg in server_ws:
                        if isinstance(msg, str):
                            await client_ws.send_text(msg)
                        else:
                            await client_ws.send_bytes(msg)
                except Exception:
                    pass
            await asyncio.gather(c2s(), s2c())
    except Exception:
        pass

# Static HTML page for /terminal
@app.get("/terminal")
async def terminal_page(request: Request):
    """Serve xterm.js terminal page."""
    from fastapi.responses import HTMLResponse
    return HTMLResponse(content=TERMINAL_HTML, media_type="text/html")
```

Add the HTML constant at top of file (or import from new `backend/app/terminal_html.py`):
```python
TERMINAL_HTML = r"""<!DOCTYPE html>
<html><head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
  <title>OpenCode Terminal</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css">
  <style>html,body,#term{height:100%;margin:0;background:#000;color:#c8c8c8;}
         .topbar{height:36px;background:#0a0a0a;border-bottom:1px solid #1a1a1a;
                 display:flex;align-items:center;padding:0 12px;gap:10px;}
         .topbar-title{color:#555;font-size:11px;text-transform:uppercase;}
         #term{position:absolute;top:36px;left:0;right:0;bottom:0;}
  </style>
</head><body>
<div class="topbar"><span class="topbar-title">opencode terminal</span></div>
<div id="term"></div>
<script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js"></script>
<script>
(() => {
  const term = new Terminal({cursorBlink:true,fontSize:13,fontFamily:'JetBrains Mono, monospace',
                             convertEol:true,scrollback:10000});
  const fit = new FitAddon.FitAddon(); term.loadAddon(fit);
  term.open(document.getElementById('term')); fit.fit(); window.addEventListener('resize',()=>fit.fit());
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/terminal/ws`);
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => console.log('Terminal WS connected');
  ws.onmessage = (e) => term.write(new Uint8Array(e.data));
  ws.onclose = () => term.write('\r\n\x1b[31m[Disconnected]\x1b[0m\r\n');
  ws.onerror = (e) => term.write('\r\n\x1b[31m[Error]\x1b[0m\r\n');
  term.onData((d) => ws.readyState === 1 && ws.send(d));
})();
</script></body></html>"""
```

### 4. Webapp HTML — add Terminal tab/button
**File:** `backend/app/webapp_html.py`

In topbar (around line 200), add:
```html
<button onclick="openTerminal()" style="background:#22c55e;border:none;color:#000;font-size:10px;font-weight:700;padding:4px 10px;border-radius:4px;cursor:pointer;text-transform:uppercase;font-family:'JetBrains Mono',monospace;">TERMINAL</button>
```

Add JS function:
```javascript
window.openTerminal = function() {
  window.open('/terminal', '_blank', 'noopener,noreferrer');
};
```
(Or embed in a tab/iframe if single-page app preferred.)

### 5. Docker Compose / HF Config — no changes needed
Port 7860 remains the only exposed port; 7681 is internal. HF Spaces reads `app_port: 7860` from README metadata (already correct).

### 6. .env.example — document new optional vars
```bash
# Terminal
TTYD_PORT=7681
TTYD_EXTRA_ARGS=  # e.g. "--signal 1 --reconnect 5"
```

## Dependency Comparison

| Package | Opencode‑Cli | OpenCode‑Serve (current) | After Integration |
|---------|--------------|--------------------------|-------------------|
| python | 3 (cleaner only) | 3.12 (full stack) | 3.12 |
| opencode binary | v1.18.3 | v1.18.3 | v1.18.3 |
| ttyd | — | — | **1.7.7** (new) |
| curl/ca-certificates | ✓ | ✓ | ✓ |
| tmux | — | ✓ | ✓ |
| supervisor | — | ✓ | ✓ |
| fastapi/uvicorn/websockets/httpx | — | ✓ | ✓ |
| python-telegram-bot | — | ✓ | ✓ |
| redis/sqlalchemy/aiosqlite | — | ✓ | ✓ |

No version conflicts. `ttyd` is a standalone binary.

## Conflict Resolution Plan

| Area | Conflict | Resolution |
|------|----------|------------|
| **Ports** | 4096 (opencode), 7860 (uvicorn), 7681 (ttyd) | All internal except 7860. No conflict. |
| **Process supervision** | Entrypoint runs opencode + uvicorn in same shell; ttyd adds 3rd bg process | Acceptable for HF Spaces; if ttyd dies, terminal tab fails but rest survives. Add `wait` trap for clean shutdown. |
| **Authentication** | HTTP Basic for opencode; Telegram whitelist | Terminal tab inherits browser session (same-origin). No extra auth needed. |
| **Workspace isolation** | Per-user tmux sessions; ttyd gives raw bash login shell | Phase 1: raw bash (simpler). Phase 2: `ttyd tmux attach -t opencode_user_<id>` behind a `/terminal/ws/{user_id}` route. |
| **Env vars** | `WORKSPACE_PATH`, `HOME=/data`, `OPENCODE_SERVER_*` | `ttyd` inherits entrypoint env automatically (same process tree). |
| **Docker layers** | `apt-get install tmux curl ...` | Add `ttyd` download in same RUN layer to avoid extra layer. |

## Startup Sequence (Updated)

```
entrypoint.sh
 ├─ mkdir /data/{workspaces,bin,logs}
 ├─ prepopulate workspaces/
 ├─ opencode serve --port 4096 &  (PID1)
 ├─ wait 45s for /global/health
 ├─ ttyd -p 7681 -i 127.0.0.1 bash -l &  (PID2)
 └─ exec uvicorn ... --port 7860         (PID0, foreground)
```

## Phase 2 Enhancement (Post-Validation): Shared tmux Session

If Phase 1 passes, upgrade the terminal to attach the user's tmux session:
```python
# In main.py, make /terminal/ws/{user_id} route
@app.websocket("/terminal/ws/{user_id}")
async def terminal_ws_user(client_ws: WebSocket, user_id: str):
    await client_ws.accept()
    # ttyd can be started per-user on dynamic port, OR
    # single ttyd with `ttyd tmux attach -t opencode_user_{user_id}`
    # Simpler: run one ttyd per user on dynamic port (4100-4999 range)
```

## Risk Mitigation
- **ttyd crash** → only terminal tab affected; chat/Telegram/API unchanged
- **Port collision** → 7681 is fixed internal; no HF exposure
- **File descriptor leaks** → ttyd is mature; uses single pty per connection
- **Security** → Same-origin policy protects `/terminal/ws`; no new auth surface
- **HF Spaces reboot** → Entrypoint re-runs, tmux sessions recreated, ttyd restarts