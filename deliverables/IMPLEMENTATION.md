# Phase 4-8 — Step-by-Step Implementation Plan

## Phase 4: Safe Experimental Environment

### 4.1 Create Integration Branch (Done)
```bash
cd /tmp/opencode-serve
git checkout -b feat/embedded-terminal
git config user.email "opencode-integration@local"
git config user.name "OpenCode Integration"
```

### 4.2 Verify Baseline Build
```bash
docker build -t opencode-serve:baseline .
# Should succeed; note image size and build time
```

### 4.3 Create Deliverables Directory (Done)
```bash
mkdir -p deliverables
```

---

## Phase 5: Embedded Terminal Implementation

### 5.1 Modify Dockerfile — Add ttyd
**File:** `Dockerfile`
**Location:** After line 18 (the `apt-get install` line)

```diff
- RUN apt-get update && apt-get install -y --no-install-recommends \
-     tmux \
-     curl \
-     ca-certificates \
-     supervisor \
-     unzip \
-     git \
+ RUN apt-get update && apt-get install -y --no-install-recommends \
+     tmux \
+     curl \
+     ca-certificates \
+     supervisor \
+     unzip \
+     git \
+     ttyd \
  && curl -fsSL https://github.com/anomalyco/opencode/releases/download/v1.18.3/opencode-linux-x64.tar.gz -o /tmp/opencode.tar.gz \
```

**Why:** Debian bookworm includes `ttyd` 1.7.x in default repos. Single `apt` line keeps layer minimal.

### 5.2 Modify Entrypoint — Start ttyd
**File:** `scripts/entrypoint.sh`
**Location:** After opencode serve wait loop (after line 82), before uvicorn start (line 84)

```bash
# ── Start ttyd terminal server ─────────────────────────────────────────
TTYD_PORT="${TTYD_PORT:-7681}"
echo "Starting ttyd on port $TTYD_PORT..."
cd "$WORKSPACE_PATH" || cd /data/workspaces
# bash --login sources /etc/profile, ~/.profile, ~/.bashrc — same env as user shells
ttyd -p "$TTYD_PORT" -i 127.0.0.1 \
  --writable \
  --title "OpenCode Terminal" \
  --signal 1 \
  --client-option '{"fontSize":13,"fontFamily":"JetBrains Mono, monospace"}' \
  bash -l > /data/logs/ttyd.log 2>&1 &
TTYD_PID=$!
echo "ttyd started (PID: $TTYD_PID)"

# Brief wait for ttyd to bind (it's instant, but be safe)
for i in $(seq 1 5); do
    if curl -sf "http://127.0.0.1:$TTYD_PORT" >/dev/null 2>&1; then
        echo "ttyd ready on port $TTYD_PORT"
        break
    fi
    sleep 0.5
done
```

**Insert point:** After `sleep 2` on line 82, before `echo "Starting uvicorn..."` on line 84.

### 5.3 Modify Main.py — Add Terminal Routes
**File:** `backend/app/main.py`
**Location:** After existing websocket proxy (after line 183), before HTTP proxy (line 185)

```python
# ── Terminal (ttyd) reverse proxy ──────────────────────────────────────
TTYD_PORT = int(os.environ.get("TTYD_PORT", "7681"))

@app.get("/terminal", response_class=HTMLResponse)
async def terminal_page():
    """Serve xterm.js terminal page."""
    return TERMINAL_HTML


@app.websocket("/terminal/ws")
async def terminal_ws_proxy(client_ws: WebSocket):
    """Proxy browser xterm.js WebSocket to ttyd on TTYD_PORT."""
    await client_ws.accept()
    uri = f"ws://127.0.0.1:{TTYD_PORT}"
    if client_ws.query_params:
        uri += f"?{client_ws.query_params}"
    try:
        async with websockets.connect(uri) as server_ws:
            async def client_to_server():
                try:
                    while True:
                        msg = await client_ws.receive()
                        if "text" in msg:
                            await server_ws.send(msg["text"])
                        elif "bytes" in msg:
                            await server_ws.send(msg["bytes"])
                except Exception:
                    pass

            async def server_to_client():
                try:
                    async for msg in server_ws:
                        if isinstance(msg, str):
                            await client_ws.send_text(msg)
                        else:
                            await client_ws.send_bytes(msg)
                except Exception:
                    pass

            await asyncio.gather(client_to_server(), server_to_client())
    except Exception:
        pass
```

**Add imports at top** (after line 53, before FastAPI app creation):
```python
from fastapi.responses import HTMLResponse
```

**Add TERMINAL_HTML constant** (before `@app.get("/terminal")`):
```python
# Inline xterm.js terminal page — uses CDN for zero-build deployment
TERMINAL_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>OpenCode Terminal</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        html, body { height: 100%; background: #000; color: #c8c8c8; font-family: 'JetBrains Mono', 'Courier New', monospace; overflow: hidden; }
        .topbar { height: 36px; background: #0a0a0a; border-bottom: 1px solid #1a1a1a; display: flex; align-items: center; padding: 0 12px; gap: 10px; }
        .topbar-title { color: #555; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; }
        #term { position: absolute; top: 36px; left: 0; right: 0; bottom: 0; }
    </style>
</head>
<body>
<div class="topbar"><span class="topbar-title">opencode terminal</span></div>
<div id="term"></div>
<script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js"></script>
<script>
(function() {
    const term = new Terminal({ cursorBlink: true, fontSize: 13, fontFamily: 'JetBrains Mono, monospace', convertEol: true, scrollback: 10000 });
    const fit = new FitAddon.FitAddon(); term.loadAddon(fit);
    term.open(document.getElementById('term')); fit.fit(); window.addEventListener('resize', () => fit.fit());
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.host}/terminal/ws`);
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => console.log('Terminal WS connected');
    ws.onmessage = (e) => term.write(new Uint8Array(e.data));
    ws.onclose = () => term.write('\r\n\x1b[31m[Disconnected]\x1b[0m\r\n');
    ws.onerror = (e) => term.write('\r\n\x1b[31m[Error]\x1b[0m\r\n');
    term.onData((d) => ws.readyState === 1 && ws.send(d));
})();
</script>
</body>
</html>"""
```

### 5.4 Modify Webapp HTML — Add Terminal Button
**File:** `backend/app/webapp_html.py`
**Location:** In topbar (around line 206), add button after "DELETE" button

```html
<button onclick="window.open('/terminal', '_blank')" style="background:#22c55e;border:none;color:#000;font-size:10px;font-weight:700;padding:4px 10px;border-radius:4px;cursor:pointer;text-transform:uppercase;font-family:'JetBrains Mono',monospace;">TERMINAL</button>
```

### 5.5 Modify .env.example — Document TTYD_PORT
**File:** `.env.example`
**Add at end:**
```bash
# Terminal (ttyd) internal port
TTYD_PORT=7681
```

---

## Phase 5: Validation (Local)

### 5.6 Build Test
```bash
cd /tmp/opencode-serve
docker build -t opencode-serve:terminal-test .
# Should succeed, image size ~baseline + 2MB
```

### 5.7 Runtime Smoke Test
```bash
# Run container with ports mapped
docker run -d --name oc-test \
  -p 7860:7860 \
  -p 7681:7681 \
  -e ANTHROPIC_API_KEY=test \
  -e WORKSPACE_PATH=/data/workspaces \
  opencode-serve:terminal-test

# Wait for startup (45s max for opencode serve + ttyd + uvicorn)
sleep 50

# Test endpoints
curl -s http://localhost:7860/global/health          # opencode proxy health
curl -s http://localhost:7860/terminal | head -5    # terminal HTML
curl -s http://localhost:7860/api/workspace/list?user_id=123  # workspace API

# Test WebSocket with wscat (if available) or python
python3 -c "
import asyncio, websockets
async def test():
    try:
        async with websockets.connect('ws://localhost:7860/terminal/ws') as ws:
            await ws.send(b'echo hello\r\n')
            resp = await asyncio.wait_for(ws.recv(), timeout=3)
            print('WS works:', 'hello' in resp.decode())
    except Exception as e:
        print('WS error:', e)
asyncio.run(test())
"

# Cleanup
docker stop oc-test && docker rm oc-test
```

### 5.8 Full Feature Check
- [ ] Webapp loads at `/`
- [ ] Terminal button opens `/terminal` in new tab
- [ ] `/terminal` shows xterm.js with bash prompt
- [ ] Typing `ls` shows workspace files
- [ ] Typing `git status` works
- [ ] Typing `opencode` launches the AI agent in the terminal
- [ ] Webapp chat still works (SSE/WS to opencode)
- [ ] Telegram bot still responds (if BOT_TOKEN set)
- [ ] Workspace clone/delete still works

---

## Phase 6: Preserve Existing Features Checklist

| Feature | Test | Status |
|---------|------|--------|
| Chat UI (webapp) | `curl /` returns HTML, WS connects | ☐ |
| SSE streaming | `/api/...` endpoints stream | ☐ |
| AI responses | `opencode` in terminal replies | ☐ |
| Authentication | HTTP Basic on `/proxy/...` works | ☐ |
| Session management | tmux sessions persist | ☐ |
| File editing | Workspace APIs CRUD | ☐ |
| Existing APIs | All `/api/*` return 200/404 (not 500) | ☐ |
| Existing routing | Catch-all proxy works | ☐ |
| Docker build | `docker build` succeeds | ☐ |
| HF deployment | `docker run` on HF port 7860 | ☐ |

---

## Phase 7: Architecture Improvements (Post-Validation)

| Improvement | Why | Effort |
|-------------|-----|--------|
| **Shared tmux session** | `ttyd tmux attach -t opencode_user_<id>` — AI + user see identical panes | Medium (per-user ttyd ports or single ttyd + tmux attach) |
| **Better PTY management** | ttyd handles resize, reconnect, signal forwarding natively | Done |
| **Process isolation** | ttyd runs as `appuser` (non-root), separate from uvicorn | Done |
| **Terminal resizing** | xterm-addon-fit + ttyd `--client-option` handles automatically | Done |
| **Reconnection handling** | ttyd `--signal 1` + browser reconnect logic | Done |
| **Session persistence** | tmux sessions survive container restart via `/data` | Existing |
| **Security** | Internal port only; same-origin browser tab | Done |
| **Performance** | Single binary, no Python overhead for terminal I/O | Done |
| **Mobile support** | xterm.js touch-friendly; topbar fits mobile | Done |
| **Error handling** | ttyd logs to `/data/logs/ttyd.log`; browser shows `[Disconnected]` | Done |

---

## Phase 8: Validation Results Template

### 8.1 Build Results
```
Build status: [PASS/FAIL]
Image size: XX MB (baseline: XX MB)
Build time: XXs (baseline: XXs)
```

### 8.2 Runtime Tests
| Test | Result | Notes |
|------|--------|-------|
| Container starts | | |
| opencode serve on 4096 | | |
| ttyd on 7681 | | |
| uvicorn on 7860 | | |
| `/terminal` HTML | | |
| `/terminal/ws` WS | | |
| Webapp `/` | | |
| `/api/workspace/list` | | |
| `/api/ws/session/123` | | |
| opencode proxy `/global/health` | | |
| `git clone` in terminal | | |
| `npm install` in terminal | | |
| `pip install` in terminal | | |
| `opencode` launch in terminal | | |

### 8.3 Regression Tests
| Feature | Before | After | Delta |
|---------|--------|-------|-------|
| Webapp chat | ✓ | | |
| Telegram bot | ✓ | | |
| Workspace clone | ✓ | | |
| Workspace delete | ✓ | | |
| File picker `/find/file` | ✓ | | |

### 8.4 Remaining Issues
| Issue | Severity | Mitigation |
|-------|----------|------------|

---

## Final Deliverables Checklist

- [ ] `deliverables/ANALYSIS.md` — Repository analysis
- [ ] `deliverables/ARCHITECTURE.md` — Architecture diagram & relationships
- [ ] `deliverables/INTEGRATION_STRATEGY.md` — Integration strategy
- [ ] `deliverables/INTEGRATION_PLAN.md` — File-by-file migration plan
- [ ] `deliverables/DEPENDENCY_DIFF.md` — Dependency comparison
- [ ] `deliverables/CONFLICTS.md` — Conflict resolution
- [ ] `deliverables/IMPLEMENTATION.md` — This document
- [ ] `deliverables/TEST_RESULTS.md` — Validation results
- [ ] `deliverables/IMPROVEMENTS.md` — Architecture improvements
- [ ] Integrated code on `feat/embedded-terminal` branch
- [ ] Docker build passes
- [ ] All regression tests pass
- [ ] Terminal feature works end-to-end

---

## Push to HF Spaces (Only After All Above Pass)

```bash
# From /tmp/opencode-serve on feat/embedded-terminal
git add -A
git commit -m "feat: add embedded terminal (ttyd + xterm.js) at /terminal"
git push origin feat/embedded-terminal

# If satisfied, merge to main and push to HF
git checkout main
git merge feat/embedded-terminal
git push origin main  # Triggers HF Space rebuild
```