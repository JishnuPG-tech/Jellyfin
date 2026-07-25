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
import sqlite3
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

# ─── Always write OpenCode config (port 8080, internal) ───
# Do NOT guard with "if not exists" — persistent /data storage could still
# have the old port 4096 config from before the nginx change, which would
# make OpenCode bind on 4096 instead of 8080 and break the nginx proxy.
mkdir -p /data/config/opencode
python3 <<'PYEOF'
import json
d = {
  "$schema": "https://opencode.ai/config.json",
  "server": {"port": 8080, "hostname": "127.0.0.1"},
  "model": "opencode/big-pickle"
}
json.dump(d, open("/data/config/opencode/opencode.json", "w"), indent=2)
print("[CONFIG] opencode.json written (port 8080)")
PYEOF

# ─── nginx: reverse proxy ───────────────────────────────────────────
rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true

# map must live in the http{} context; conf.d files are included there.
cat > /etc/nginx/conf.d/opencode-map.conf << 'MAPEOF'
# Correctly set Connection header:
#   WebSocket requests  → "upgrade"
#   Regular HTTP / SSE  → "close"  (not "upgrade" — that breaks SSE streaming)
map $http_upgrade $connection_upgrade {
    default  upgrade;
    ''       close;
}
MAPEOF

cat > /etc/nginx/conf.d/opencode.conf << 'NGINXEOF'
server {
    listen 7860;

    # Disable gzip globally — gzip breaks SSE (chunked streaming) responses
    gzip off;

    # ── Real PTY terminal (ttyd on :7681) ────────────────────────────
    location /terminal {
        proxy_pass              http://127.0.0.1:7681;
        proxy_http_version      1.1;
        proxy_set_header        Upgrade         $http_upgrade;
        proxy_set_header        Connection      $connection_upgrade;
        proxy_set_header        Host            $host;
        proxy_set_header        Accept-Encoding "";
        proxy_read_timeout      86400;
        proxy_buffering         off;

        # Inject mobile-friendly viewport + CSS into ttyd's HTML
        sub_filter '<head>' '<head>
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,minimum-scale=1.0,user-scalable=no,viewport-fit=cover">
<style>
html, body {
  margin: 0; padding: 0;
  width: 100%; height: 100%;
  background: #1a1b26;
  overflow: hidden;
  -webkit-text-size-adjust: 100%;
}
#terminal-container, .xterm, .xterm-screen, .xterm-viewport {
  width: 100% !important;
  height: 100% !important;
  max-width: 100% !important;
}
body { position: fixed; }
#mob-kb-btn {
  display: none;
  position: fixed; bottom: 18px; right: 18px; z-index: 9999;
  width: 52px; height: 52px; border-radius: 50%;
  background: #7aa2f7; border: none;
  box-shadow: 0 3px 10px rgba(0,0,0,.45);
  cursor: pointer; align-items: center; justify-content: center;
  font-size: 26px; color: #1a1b26;
}
@media (hover: none) and (pointer: coarse) { #mob-kb-btn { display: flex; } }
</style>
<script>
document.addEventListener("DOMContentLoaded", function () {
  var btn = document.createElement("button");
  btn.id = "mob-kb-btn"; btn.title = "Toggle keyboard"; btn.textContent = "⌨";
  btn.addEventListener("click", function () {
    var ta = document.querySelector(".xterm-helper-textarea");
    if (ta) { ta.focus(); ta.click(); }
  });
  document.body.appendChild(btn);
});
</script>';
        sub_filter_once    on;
        sub_filter_types   text/html;
    }

    # ── OpenCode chat UI + REST API + SSE streaming ───────────────────
    location / {
        proxy_pass              http://127.0.0.1:8080;
        proxy_http_version      1.1;
        proxy_set_header        Upgrade             $http_upgrade;
        proxy_set_header        Connection          $connection_upgrade;
        proxy_set_header        Host                $host;
        proxy_read_timeout      86400;

        # Critical for SSE: disable all buffering so token-by-token
        # streaming reaches the browser immediately
        proxy_buffering         off;
        proxy_cache             off;
        proxy_set_header        X-Accel-Buffering   no;
    }
}
NGINXEOF

echo "[NGINX] Testing config..."
nginx -t 2>&1
echo "[NGINX] Starting on :${PORT:-7860} ..."
nginx
echo "[NGINX] Started."

mkdir -p /projects/default
cd /projects/default
[ -d .git ] || git init -q 2>/dev/null

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

# ─── OpenCode on :8080 (nginx proxies / → here) ──────────────────────
echo "[OPENCODE] opencode serve on :8080 ..."
exec opencode serve --port 8080 --hostname 127.0.0.1
