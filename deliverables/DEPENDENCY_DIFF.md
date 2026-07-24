# Phase 3d — Dependency Comparison

## Current OpenCode-Serve Requirements (`requirements.txt`)

```
fastapi>=0.95.0
uvicorn[standard]>=0.22.0
python-telegram-bot[aio]>=20.0
httpx>=0.24.0
pexpect>=4.8.0
tmuxp>=1.0.0
aiosqlite>=0.18.0
SQLAlchemy>=1.4.36,<2.0.0
python-dotenv>=1.0.0
aiofiles>=23.0.0
pytest>=7.0.0
psutil>=5.9.0
redis>=4.5.0
python-multipart==0.0.6
pydantic==2.6.0
gunicorn==21.2.0
supervisor==4.3.0
typing-extensions
```

## System Packages (Dockerfile)
```
tmux
curl
ca-certificates
supervisor
unzip
git
```

## Added for Terminal Integration

### Runtime Binary (no PyPI package)
- **ttyd 1.7.7** — downloaded from GitHub Releases, installed to `/usr/local/bin/ttyd`
  - Static binary, no dependencies, ~2MB
  - License: MIT

### Frontend Assets (CDN, no install)
- **xterm.js 5.3.0** — `https://cdn.jsdelivr.net/npm/xterm@5.3.0/`
- **xterm-addon-fit 0.8.0** — `https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/`
- **JetBrains Mono font** — `https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700`

## Dependency Delta

| Category | Before | After | Risk |
|----------|--------|-------|------|
| Python deps | 18 packages | **Unchanged** | None |
| System packages | 6 packages | **+0** (ttyd via curl) | None |
| Runtime binaries | opencode, tmux | **+ ttyd** | Low (standalone) |
| JS assets | Inline HTML | **CDN xterm.js** | None (cached) |
| Ports exposed | 7860 | **7860 only** (7681 internal) | None |

## Version Pinning Strategy
- `ttyd` version pinned in Dockerfile URL (`1.7.7`)
- xterm.js version pinned in HTML (`5.3.0`, `0.8.0`)
- All Python deps already pinned or bounded

## Size Impact
- Docker image: +~2 MB (ttyd binary)
- Build time: +~5 seconds (curl + chmod)
- Runtime memory: +~5-10 MB (ttyd process per connection, typically 1-2)
- Zero Python dependency changes → no pip conflicts possible

## Validation Commands
```bash
# Verify ttyd works
docker run --rm <image> ttyd --version
# Should print: ttyd version 1.7.7

# Verify xterm.js loads
curl -I https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js
# Should return 200
```