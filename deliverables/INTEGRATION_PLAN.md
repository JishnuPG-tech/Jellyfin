# Phase 3 — Integration Plan

## Objective
Add a production-quality embedded terminal (`ttyd` + xterm.js) to the OpenCode-Serve webapp at `/terminal`, sharing the same container filesystem, environment, shell, and working directory as the opencode AI agent.

## Files to Add/Modify

### 1. Dockerfile — Install ttyd
**File:** `Dockerfile` (modify)
**Change:** Add ttyd binary installation in the existing `apt-get install` layer.
**Reason:** Single binary, no Python deps, industry standard for HF Spaces terminals.

### 2. Entrypoint — Start ttyd alongside opencode + uvicorn
**File:** `scripts/entrypoint.sh` (modify)
**Changes:**
- Add `TTYD_PORT=${TTYD_PORT:-7681}` default
- Start `ttyd -p $TTYD_PORT -i 127.0.0.1 bash --login` in background before uvicorn
- Wait briefly for ttyd to bind (1-2s max)
- Pass `-c "cd $WORKSPACE_PATH && exec bash --login"` so initial cwd matches workspace

### 3. FastAPI — Add /terminal static route + WS reverse proxy
**File:** `backend/app/main.py` (modify)
**Additions:**
- StaticFiles mount for `/terminal/assets` (optional) or inline HTML
- GET `/terminal` → serve xterm.js HTML (reusing pattern from `webapp_html.py`)
- WebSocket `/terminal/ws` → reverse proxy to `ws://127.0.0.1:7681`

### 4. WebApp HTML — Add Terminal Tab/Route
**File:** `backend/app/webapp_html.py` (modify)
**Changes:**
- Add "Terminal" button in topbar next to workspace selector
- On click, navigate to `/terminal` (new tab or iframe)
- Keep existing webapp functional at `/`

### 5. Config — Document new env var
**File:** `.env.example` (modify)
**Add:** `TTYD_PORT=7681`

## Migration Steps (Ordered for Safety)

### Step 1: Dockerfile (build-only change)
```dockerfile
# In existing RUN apt-get install line, add ttyd:
RUN apt-get update && apt-get install -y --no-install-recommends \
    tmux curl ca-certificates supervisor unzip git \
    ttyd \
 && ...
```
Validate: `docker build` succeeds, `ttyd --version` works in container.

### Step 2: Entrypoint (runtime change)
```bash
# After opencode serve wait loop, before uvicorn:
TTYD_PORT="${TTYD_PORT:-7681}"
echo "Starting ttyd on port $TTYD_PORT..."
cd "$WORKSPACE_PATH"
ttyd -p "$TTYD_PORT" -i 127.0.0.1 bash --login > /data/logs/ttyd.log 2>&1 &
TTYD_PID=$!
sleep 2
```
Validate: `docker run` → `curl -s http://localhost:7681` returns ttyd HTML.

### Step 3: Main.py routes (code change)
```python
# Add near other imports
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

# Add after app creation
@app.get("/terminal", response_class=HTMLResponse)
async def terminal_page():
    return TERMINAL_HTML  # imported from webapp_html or inline

@app.websocket("/terminal/ws")
async def terminal_ws_proxy(ws: WebSocket):
    await ws.accept()
    uri = f"ws://127.0.0.1:{TTYD_PORT}"
    async with websockets.connect(uri) as upstream:
        await asyncio.gather(
            client_to_upstream(ws, upstream),
            upstream_to_client(ws, upstream)
        )
```
Validate: `curl http://localhost:7860/terminal` returns HTML; WS connects.

### Step 4: WebApp UI (UI change)
Add button in `webapp_html.py` topbar:
```html
<button onclick="window.open('/terminal', '_blank')" 
        style="...">TERMINAL</button>
```
Validate: Webapp loads, button opens `/terminal` in new tab with working shell.

## File-by-File Copy/Merge Matrix

| Source | Target | Action |
|--------|--------|--------|
| `Dockerfile` | `Dockerfile` | **Modify in place** (add ttyd to apt line) |
| `scripts/entrypoint.sh` | `scripts/entrypoint.sh` | **Modify in place** (add ttyd startup) |
| `backend/app/main.py` | `backend/app/main.py` | **Modify in place** (add 2 routes) |
| `backend/app/webapp_html.py` | `backend/app/webapp_html.py` | **Modify in place** (add terminal button) |
| `.env.example` | `.env.example` | **Modify in place** (add TTYD_PORT) |
| `requirements.txt` | `requirements.txt` | **No change** |
| `docker-compose.yml` | `docker-compose.yml` | **No change** (port 7860 only) |

## Configuration Conflicts & Resolutions

| Conflict | Resolution |
|----------|------------|
| Port collision | ttyd binds 127.0.0.1:7681 (internal only); HF exposes 7860 only |
| Auth | Terminal is same-origin browser tab → no extra auth needed |
| Working directory | ttyd launched from `$WORKSPACE_PATH` in entrypoint | Matches webapp |
| Environment | Entrypoint exports all vars before ttyd spawn | Inherited automatically |
| Permissions | `appuser` owns `/data`; ttyd runs as `appuser` | No sudo needed |

## Dependencies

| Package | Source | Version Pin | Why |
|---------|--------|-------------|-----|
| ttyd | Debian bookworm `ttyd` package | 1.7.x (distro default) | Stable, no compile needed |
| websockets | Already in `requirements.txt` (transitive via `python-telegram-bot` or explicit) | Already present | Used by new `/terminal/ws` proxy |

*No new Python packages required.* `websockets` is already a dependency of `python-telegram-bot[aio]` or can be confirmed with `pip check`.

## Validation Checklist

| Test | Command | Expected |
|------|---------|----------|
| Docker build | `docker build -t oc-serve-test .` | SUCCESS |
| ttyd binary | `docker run --rm oc-serve-test ttyd --version` | 1.7.x |
| Entrypoint dry-run | `docker run --rm oc-serve-test /app/scripts/entrypoint.sh --dry-run` | No syntax errors |
| Full stack | `docker run -d -p 7860:7860 -p 7681:7681 oc-serve-test` | All 3 processes up |
| HTTP /terminal | `curl -s http://localhost:7860/terminal` | xterm.js HTML |
| WS /terminal/ws | `wscat -c ws://localhost:7860/terminal/ws` | Connected, shell prompt |
| Webapp loads | `curl -s http://localhost:7860` | Existing HTML |
| opencode proxy | `curl -s http://localhost:7860/global/health` | 200 OK |
| Workspace API | `curl -s http://localhost:7860/api/workspace/list?user_id=123` | JSON folders |

## Rollback Procedure
```bash
git checkout main -- Dockerfile scripts/entrypoint.sh backend/app/main.py backend/app/webapp_html.py .env.example
docker build -t oc-serve-rollback .
# Redeploy to HF Space
```