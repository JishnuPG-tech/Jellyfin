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
echo "[INIT] Setting up /data directories..."
mkdir -p /data/share/opencode 2>/dev/null || echo "[WARN] Could not create /data/share/opencode"
mkdir -p /data/config/opencode 2>/dev/null || echo "[WARN] Could not create /data/config/opencode"
mkdir -p /data/cache/opencode 2>/dev/null || echo "[WARN] Could not create /data/cache/opencode"
mkdir -p /data/state/opencode 2>/dev/null || echo "[WARN] Could not create /data/state/opencode"
mkdir -p /data/workspaces /data/logs 2>/dev/null || true

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
# Philosophy: match the working Opencode-Cli space as closely as possible.
# That space runs OpenCode directly with no proxy. Here we only add nginx
# to route /terminal to ttyd. Everything else goes straight to OpenCode.
# No sub_filter, no CSP changes, no localStorage injection — those were
# the root cause of "send button does nothing" (SPA failed to initialize).
rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true

cat > /etc/nginx/conf.d/opencode-map.conf << 'MAPEOF'
map $http_upgrade $connection_upgrade {
    websocket  upgrade;
    default    "";
}
MAPEOF

cat > /etc/nginx/conf.d/opencode.conf << 'NGINXEOF'
server {
    listen 7860;

    # Disable gzip — compressed SSE cannot be decompressed incrementally
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

        # Mobile-friendly: inject viewport and keyboard FAB into ttyd HTML
        sub_filter '<head>' '<head>
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,minimum-scale=1.0,user-scalable=no,viewport-fit=cover">
<style>
html, body { margin: 0; padding: 0; width: 100%; height: 100%; background: #1a1b26; overflow: hidden; -webkit-text-size-adjust: 100%; }
#terminal-container, .xterm, .xterm-screen, .xterm-viewport { width: 100% !important; height: 100% !important; max-width: 100% !important; }
body { position: fixed; }
#mob-kb-btn { display: none; position: fixed; bottom: 18px; right: 18px; z-index: 9999; width: 52px; height: 52px; border-radius: 50%; background: #7aa2f7; border: none; box-shadow: 0 3px 10px rgba(0,0,0,.45); cursor: pointer; align-items: center; justify-content: center; font-size: 26px; color: #1a1b26; }
@media (hover: none) and (pointer: coarse) { #mob-kb-btn { display: flex; } }
</style>
<script>document.addEventListener("DOMContentLoaded",function(){var b=document.createElement("button");b.id="mob-kb-btn";b.title="Toggle keyboard";b.textContent="\u2328";b.addEventListener("click",function(){var t=document.querySelector(".xterm-helper-textarea");if(t){t.focus();t.click();}});document.body.appendChild(b);});</script>';
        sub_filter_once    on;
    }

    # ── OpenCode (all paths except /terminal) ─────────────────────────
    # proxy_buffering MUST be off so SSE streams token-by-token.
    # OpenCode's API lives at /session/*, /event, /api/*, /config,
    # /permission, /question, /file, /find — all served by the same
    # OpenCode process. A single catch-all location is simpler and safer
    # than trying to enumerate every API path.
    location / {
        proxy_pass              http://127.0.0.1:8080;
        proxy_http_version      1.1;
        proxy_set_header        Upgrade             $http_upgrade;
        proxy_set_header        Connection          $connection_upgrade;
        proxy_set_header        Host                $host;
        proxy_set_header        Accept-Encoding     "";
        proxy_read_timeout      86400;
        proxy_buffering         off;
        proxy_cache             off;
    }
}
NGINXEOF

echo "[NGINX] Testing config..."
nginx -t 2>&1
echo "[NGINX] Starting on :7860 ..."
nginx
echo "[NGINX] Started."

# ─── Start DB self-healing daemon ────────────────────────────────────
echo "[CLEANER] Starting self-healing daemon..."
python3 /cleaner.py &

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
