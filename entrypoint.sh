#!/bin/sh
# OpenCode-Serve entrypoint
#
#  nginx    :7860  (HF exposed)
#    /terminal  → ttyd  :7681  — real PTY bash, mobile-optimised
#    /          → opencode :8080 — chat UI + REST API + SSE
#  sshd     :22   (internal only)
#    ↑ exposed via bore transparent TCP tunnel
#      Termius → bore.pub:PORT → raw TCP → sshd:22
#      (no SSH interception = full PTY, arrow keys, Ctrl+C, vim)
#
set -u

echo "============================================"
echo "=== OpenCode-Serve starting               ==="
echo "Time: $(date)"
echo "============================================"

# Prevent git ownership errors
git config --global --add safe.directory '*' 2>/dev/null || true

# ─── Data directories ───
echo "[INIT] Setting up /data directories..."
mkdir -p /data/share/opencode 2>/dev/null || echo "[WARN] Could not create /data/share/opencode"
mkdir -p /data/config/opencode 2>/dev/null || echo "[WARN] Could not create /data/config/opencode"
mkdir -p /data/cache/opencode 2>/dev/null || echo "[WARN] Could not create /data/cache/opencode"
mkdir -p /data/state/opencode 2>/dev/null || echo "[WARN] Could not create /data/state/opencode"
mkdir -p /data/workspaces /data/logs /root/.ssh 2>/dev/null || true
chmod 700 /root/.ssh

# ─── Restore persistent storage from HF Dataset ──────────────────────
echo "[RESTORE] Restoring workspace from HF Dataset..."
python3 /sync_engine.py restore 2>&1 | tee -a /data/logs/sync.log
echo "[RESTORE] Done."

# ─── OpenCode config ─────────────────────────────────────────────────
echo "[CONFIG] Setting up default configuration..."
python3 -c "
import json, os
p = '/data/config/opencode/opencode.json'
stale_models = ['big-pickle', 'mimo-v2.5-free', 'opencode/mimo-v2.5-free']
try:
    d = json.load(open(p)) if os.path.exists(p) else {}
    if d.get('model') in stale_models:
        print(f'[CONFIG] Removing stale model {d[\"model\"]!r}, will regenerate')
        del d['model']
        json.dump(d, open(p, 'w'), indent=2)
except Exception as e:
    print(f'[CONFIG] Error normalizing: {e}')
" 2>/dev/null || true

python3 -c "
import json, os
p = '/data/config/opencode/opencode.json'
try:
    d = json.load(open(p)) if os.path.exists(p) else {}
except Exception:
    d = {}
d['\$schema'] = 'https://opencode.ai/config.json'
d['server'] = {'port': 8080, 'hostname': '127.0.0.1'}
if not os.environ.get('ANTHROPIC_API_KEY') and not os.environ.get('OPENAI_API_KEY'):
    d['model'] = 'opencode/big-pickle'
elif not d.get('model'):
    d['model'] = 'opencode/big-pickle'
json.dump(d, open(p, 'w'), indent=2)
print('[CONFIG] Wrote base config with model:', d.get('model'))
" || true

# ─── Bootstrap persistent memory directory ───────────────────────────────────
# Creates /projects/default/memory/{GLOBAL,PROJECT,CONVENTIONS,TODO}.md with
# starter templates if they don't exist yet.  Existing files are never touched.
echo "[MEMORY] Bootstrapping memory directory..."
mkdir -p /projects/default/memory/sessions
python3 - << 'INIT_MEMORY'
from pathlib import Path

MEMORY_DIR   = Path("/projects/default/memory")
SESSIONS_DIR = MEMORY_DIR / "sessions"
MEMORY_DIR.mkdir(parents=True, exist_ok=True)
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

templates = {
    "GLOBAL.md": """\
# Global Memory

## User Preferences
<!-- Add coding style preferences, editor settings, workflow habits -->

## Preferred Technologies
<!-- Add preferred languages, frameworks, libraries, tools -->

## Formatting Rules
<!-- Add code formatting preferences, line length, indentation style -->

## Reusable Patterns
<!-- Add patterns, snippets, or approaches to reuse across projects -->

## Communication Style
<!-- How the user likes responses: concise / detailed, with examples / without, etc. -->
""",
    "PROJECT.md": """\
# Project Memory

## Project Overview
<!-- Briefly describe what this project does and its main goals -->

## Architecture
<!-- Describe the high-level architecture, key components, and how they interact -->

## Tech Stack
<!-- List languages, frameworks, databases, external services -->

## APIs & Interfaces
<!-- Document important APIs, endpoints, schemas, or interfaces -->

## Design Decisions
<!-- Record important architectural or design choices and why they were made -->

## Completed Features
<!-- List features that have been implemented -->

## Known Issues
<!-- Record known bugs, limitations, or technical debt -->
""",
    "CONVENTIONS.md": """\
# Coding Conventions

## Naming
<!-- Variable, function, class, file naming rules -->

## Code Style
<!-- Formatting, linting, documentation standards -->

## Patterns & Anti-patterns
<!-- Project-specific patterns to follow or avoid -->

## Testing
<!-- Testing approach, coverage expectations, test naming -->

## Git & Workflow
<!-- Branch naming, commit message format, PR process -->
""",
    "TODO.md": """\
# Pending Tasks

## High Priority
<!-- Critical tasks that need immediate attention -->

## In Progress
<!-- Tasks currently being worked on -->

## Backlog
<!-- Future features, improvements, ideas -->

## Completed (recent)
<!-- Recently completed tasks — remove when no longer relevant -->
""",
}

for name, content in templates.items():
    fp = MEMORY_DIR / name
    if not fp.exists():
        fp.write_text(content, encoding="utf-8")
        print(f"[MEMORY] Created template: memory/{name}")
    else:
        print(f"[MEMORY] Existing:  memory/{name} ({fp.stat().st_size} bytes)")
INIT_MEMORY

# ─── Load persistent memory → opencode.json instructions ──────────────────────
# memory_updater.py assembles all memory sources (GLOBAL.md, PROJECT.md,
# CONVENTIONS.md, TODO.md, recent session summaries, workspace auto-scan)
# into the `instructions` field of opencode.json.  OpenCode injects this
# as a system-level prompt for every new conversation automatically.
echo "[MEMORY] Assembling memory context → opencode.json..."
python3 /memory_updater.py once

echo "[CONFIG] Current configuration:"
cat /data/config/opencode/opencode.json 2>/dev/null || echo "{}"

# ─── Detect and remove malformed SQLite databases ───
echo "[DB] Checking database integrity..."
DB_PATH="/data/share/opencode/opencode.db"
if [ -f "$DB_PATH" ]; then
    echo "[DB] Found database at $DB_PATH"
    python3 -c "
import sqlite3, os, glob
try:
    conn = sqlite3.connect('$DB_PATH', timeout=5)
    row = conn.execute('PRAGMA integrity_check').fetchone()
    conn.close()
    if not row or row[0] != 'ok':
        raise ValueError(f'Integrity check failed: {row}')
    print('[DB] Integrity check passed: OK')
except Exception as e:
    print(f'[DB] Error: {e} — removing corrupt database files')
    for f in glob.glob('$DB_PATH*'):
        try:
            os.remove(f)
            print(f'[DB] Removed corrupt file: {f}')
        except Exception as ex:
            print(f'[DB] Failed to remove {f}: {ex}')
" 2>/dev/null || echo "[DB] Could not check database"
else
    echo "[DB] No database found (fresh start)"
fi

# ─── nginx: minimal reverse proxy ───────────────────────────────────
cat > /etc/nginx/nginx.conf << 'NGINX_CONF'
events { worker_connections 1024; }
http {
    include       /etc/nginx/mime.types;
    default_type  application/octet-stream;

    server {
        listen 7860;

        # Root: serve OpenCode chat UI directly.
        # Returns 200 with a client-side redirect so HF Spaces iframe proxies
        # (which may swallow HTTP 3xx responses) still land on the chat.
        location = / {
            default_type text/html;
            return 200 '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>OpenCode</title><script>window.location.replace("/server")</script><meta http-equiv="refresh" content="0;url=/server"></head><body style="margin:0;background:#09090b;color:#a1a1aa;font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh"><p>Loading OpenCode...</p></body></html>';
        }

        # Terminal (ttyd PTY)
        location /terminal {
            proxy_pass         http://127.0.0.1:7681;
            proxy_http_version 1.1;
            proxy_set_header   Upgrade $http_upgrade;
            proxy_set_header   Connection "upgrade";
            proxy_set_header   Host $host;
            proxy_read_timeout 86400;
        }

        # OpenCode — everything else
        location / {
            proxy_pass         http://127.0.0.1:8080;
            proxy_http_version 1.1;
            proxy_set_header   Upgrade $http_upgrade;
            proxy_set_header   Connection $http_connection;
            proxy_set_header   Host $host;
            proxy_set_header   X-Real-IP $remote_addr;
            proxy_buffering    off;
            proxy_read_timeout 86400;
        }
    }
}
NGINX_CONF
nginx
echo "[NGINX] Started."

# ─── Start DB self-healing daemon ────────────────────────────────────
echo "[CLEANER] Starting self-healing daemon..."
python3 /cleaner.py &

# ─── Start background sync daemon ────────────────────────────────────
echo "[SYNC] Starting background sync daemon..."
python3 /sync_engine.py watch 2>&1 | tee -a /data/logs/sync.log &
echo "[SYNC] Sync daemon started. Logs: /data/logs/sync.log"

# ─── Start memory live-watcher ───────────────────────────────────────────────
# Watches memory/GLOBAL.md, memory/PROJECT.md, memory/CONVENTIONS.md,
# memory/TODO.md, memory/sessions/, and workspace files (README, package.json…)
# every 15 seconds.  When any source changes, re-assembles the full memory
# context and updates opencode.json so the NEXT new session picks it up.
echo "[MEMORY] Starting memory live-watcher..."
python3 /memory_updater.py watch 2>&1 | tee -a /data/logs/memory.log &
echo "[MEMORY] Live-watcher started. Logs: /data/logs/memory.log"

# ─── Start session summariser daemon ─────────────────────────────────────────
# Polls OpenCode's SQLite DB every 30 seconds.  When a conversation has been
# idle for 5+ minutes and has at least 2 user messages, it writes a compact
# markdown summary to /projects/default/memory/sessions/YYYY-MM-DD_HH-MM_ID.md.
# The memory watcher picks up new summaries within 15 seconds and injects them
# into the system prompt for the next conversation.
echo "[SESSION] Starting session summariser daemon..."
python3 /session_watcher.py 2>&1 | tee -a /data/logs/sessions.log &
echo "[SESSION] Session summariser started. Logs: /data/logs/sessions.log"

# ─── Ensure project dir exists ───────────────────────────────────────
mkdir -p /projects/default
cd /projects/default
[ -d .git ] || git init -q 2>/dev/null || true

# ─── ttyd: real PTY bash on :7681 ────────────────────────────────────
echo "[TERMINAL] ttyd on :7681 (base-path /terminal) ..."
nohup ttyd -p 7681 -i 0.0.0.0 \
  -b /terminal \
  -W \
  -t fontSize=15 \
  -t lineHeight=1.1 \
  -t cursorBlink=true \
  -t scrollback=2000 \
  bash -l > /data/logs/ttyd.log 2>&1 &

# ─── Test OpenCode Zen API reachability ─────────────────────────────
echo "[NET] Testing OpenCode Zen API..."
ZEN_STATUS=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 "https://opencode.ai/zen/v1/models" 2>/dev/null || echo "000")
echo "[NET] OpenCode Zen API: $ZEN_STATUS"
if [ "$ZEN_STATUS" != "200" ]; then
    echo "[NET] WARNING: Zen API unreachable — free model responses may fail"
fi

# ─── OpenCode on :8080 (nginx proxies / → here) ──────────────────────
echo "[OPENCODE] opencode serve on :8080 ..."
exec opencode serve --port 8080 --hostname 127.0.0.1
