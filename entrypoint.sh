#!/bin/sh
# OpenCode-Serve entrypoint
#
#  nginx  :7860  (HF exposed)
#    /terminal  → ttyd  :7681  — real PTY bash, mobile-optimised
#    /          → opencode :8080 — chat UI + REST API + SSE
#
set -u

echo "============================================"
echo "=== OpenCode-Serve starting               ==="
echo "Time: $(date)"
echo "============================================"

# Prevent git ownership errors
git config --global --add safe.directory '*' 2>/dev/null || true

# ─── Data directories ───
# /data is no longer a mounted volume — sync_engine restores its contents
# from the HF Dataset repo below. We create the dirs here so every
# subsequent step has a guaranteed path to write to.
echo "[INIT] Setting up /data directories..."
mkdir -p /data/share/opencode 2>/dev/null || echo "[WARN] Could not create /data/share/opencode"
mkdir -p /data/config/opencode 2>/dev/null || echo "[WARN] Could not create /data/config/opencode"
mkdir -p /data/cache/opencode 2>/dev/null || echo "[WARN] Could not create /data/cache/opencode"
mkdir -p /data/state/opencode 2>/dev/null || echo "[WARN] Could not create /data/state/opencode"
mkdir -p /data/workspaces /data/logs 2>/dev/null || true

# ─── Restore persistent storage from HF Dataset ──────────────────────
# This pulls /projects/default (workspace), /data/share/opencode (OpenCode DB),
# and /data/config/opencode (OpenCode config) from the dataset repo so the user
# continues exactly where they left off after a container rebuild.
echo "[RESTORE] Restoring workspace from HF Dataset..."
python3 /sync_engine.py restore 2>&1 | tee -a /data/logs/sync.log
echo "[RESTORE] Done."

# ─── OpenCode config ─────────────────────────────────────────────────
# Remove stale model configs that use wrong format
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

# Always write the server block so OpenCode binds on port 8080 (nginx proxies to it)
# and the model is set correctly. The server is behind nginx so hostname is 127.0.0.1.
python3 -c "
import json, os
p = '/data/config/opencode/opencode.json'
try:
    d = json.load(open(p)) if os.path.exists(p) else {}
except Exception:
    d = {}
d['\$schema'] = 'https://opencode.ai/config.json'
# Always overwrite port/hostname — old containers may have stored port 4096
d['server'] = {'port': 8080, 'hostname': '127.0.0.1'}
if not os.environ.get('ANTHROPIC_API_KEY') and not os.environ.get('OPENAI_API_KEY'):
    d['model'] = 'opencode/big-pickle'
elif not d.get('model'):
    d['model'] = 'opencode/big-pickle'
json.dump(d, open(p, 'w'), indent=2)
print('[CONFIG] Wrote config with model:', d.get('model'))
" 2>/dev/null || true
echo "[CONFIG] Current configuration:"
cat /data/config/opencode/opencode.json 2>/dev/null || echo "{}"

# ─── Detect and remove malformed SQLite databases ───
echo "[DB] Checking database integrity..."
DB_PATH="/data/share/opencode/opencode.db"
if [ -f "$DB_PATH" ]; then
    echo "[DB] Found database at $DB_PATH"
    python3 -c "
import sqlite3
try:
    conn = sqlite3.connect('$DB_PATH', timeout=5)
    row = conn.execute('PRAGMA integrity_check').fetchone()
    conn.close()
    print('[DB] Integrity:', row[0] if row else 'error')
except Exception as e:
    print('[DB] Error:', e)
" 2>/dev/null || echo "[DB] Could not read database"
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
# Runs every 15 s, commits only changed files to the HF Dataset.
# The AI and terminal always work on the local filesystem — sync is
# purely a background backup layer and never blocks any operation.
echo "[SYNC] Starting background sync daemon..."
python3 /sync_engine.py watch 2>&1 | tee -a /data/logs/sync.log &
echo "[SYNC] Sync daemon started. Logs: /data/logs/sync.log"

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
