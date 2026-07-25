#!/bin/sh
# OpenCode-Serve entrypoint
#
#  nginx  :7860  (HF exposed)
#    /terminal  → ttyd  :7681  — real PTY bash over WebSocket
#    /          → opencode :8080 — chat UI + REST API
#
set -u

echo "============================================"
echo "=== OpenCode-Serve starting               ==="
echo "Time: $(date)"
echo "============================================"

mkdir -p /data/share/opencode /data/config/opencode /data/cache/opencode /data/state/opencode \
         /data/workspaces /data/logs \
 2>/dev/null || true

# ─── Detect and remove malformed SQLite databases ───
echo "[DB] Checking database integrity..."
DB_PATHS="/data/share/opencode/opencode.db \
          /projects/.opencode/share/opencode/opencode.db \
          /root/.local/share/opencode/opencode.db \
          /home/opencode/.local/share/opencode/opencode.db"

for db_path in $DB_PATHS; do
  if [ -f "$db_path" ]; then
    result=$(python3 -c "
import sqlite3, sys
try:
    conn = sqlite3.connect('$db_path', timeout=3)
    row = conn.execute('PRAGMA integrity_check').fetchone()
    conn.close()
    print(row[0] if row else 'error')
except Exception as e:
    print('error: ' + str(e))
" 2>/dev/null || echo "error")
    if [ "$result" != "ok" ]; then
      echo "[DB] Malformed database at $db_path ($result) — removing."
      rm -f "$db_path" "${db_path}-wal" "${db_path}-shm" 2>/dev/null || true
    else
      echo "[DB] OK: $db_path"
    fi
  fi
done

# ─── OpenCode config (port 8080, internal) ───
if [ ! -f /data/config/opencode/opencode.json ]; then
  mkdir -p /data/config/opencode
  python3 <<'PYEOF' 2>/dev/null || true
import json
d = {
  "$schema": "https://opencode.ai/config.json",
  "server": {"port": 8080, "hostname": "127.0.0.1"},
  "model": "opencode/big-pickle"
}
json.dump(d, open("/data/config/opencode/opencode.json", "w"), indent=2)
PYEOF
fi

# ─── nginx config ───
# Remove default site, write our reverse-proxy config
rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true

cat > /etc/nginx/conf.d/opencode.conf << 'NGINXEOF'
server {
    listen 7860;

    # ── Real PTY terminal via ttyd ──────────────────────────────────
    # ttyd runs on :7681 with --base-path /terminal
    # Its HTML/JS uses absolute paths like /terminal/ws so nginx must
    # NOT strip the prefix — proxy the full path straight through.
    location /terminal {
        proxy_pass         http://127.0.0.1:7681;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade    $http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_set_header   Host       $host;
        proxy_read_timeout 86400;
        proxy_buffering    off;
    }

    # ── OpenCode chat UI + REST API ─────────────────────────────────
    location / {
        proxy_pass         http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade    $http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_set_header   Host       $host;
        proxy_read_timeout 86400;
        proxy_buffering    off;
    }
}
NGINXEOF

echo "[NGINX] Starting on :${PORT:-7860} ..."
nginx
echo "[NGINX] Started."

mkdir -p /projects/default
cd /projects/default
[ -d .git ] || git init -q 2>/dev/null

# ─── ttyd: real PTY bash on :7681, base-path /terminal ───
# -W   = write-mode (allow input)
# -b   = base-path so generated JS uses /terminal/ws for its WebSocket
# -i   = bind to all interfaces (nginx proxies from loopback)
echo "[TERMINAL] ttyd on :7681 (base-path /terminal) ..."
nohup ttyd -p 7681 -i 0.0.0.0 -b /terminal -W \
  bash -l > /data/logs/ttyd.log 2>&1 &

# ─── OpenCode on :8080 (internal, nginx proxies /) ───
echo "[OPENCODE] opencode serve on :8080 ..."
exec opencode serve --port 8080 --hostname 127.0.0.1
