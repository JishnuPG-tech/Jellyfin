#!/bin/sh
# OpenCode-Serve entrypoint
#
#  nginx  :7860  (HF exposed)
#    /terminal  → ttyd  :7681  — real PTY bash, mobile-optimised
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

# ─── OpenCode config (internal port 8080) ───
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

# ─── nginx: reverse proxy + mobile terminal injection ───
rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true

cat > /etc/nginx/conf.d/opencode.conf << 'NGINXEOF'
server {
    listen 7860;

    # ── Terminal (ttyd on :7681) ──────────────────────────────────────
    # sub_filter injects a mobile viewport + CSS into ttyd's HTML so the
    # terminal fills the screen at the right font size on phones/tablets.
    # Accept-Encoding "" stops ttyd gzip-compressing the page (sub_filter
    # needs the raw text to work).
    location /terminal {
        proxy_pass              http://127.0.0.1:7681;
        proxy_http_version      1.1;
        proxy_set_header        Upgrade         $http_upgrade;
        proxy_set_header        Connection      "upgrade";
        proxy_set_header        Host            $host;
        proxy_set_header        Accept-Encoding "";
        proxy_read_timeout      86400;
        proxy_buffering         off;

        # Inject mobile-friendly head into ttyd's page
        sub_filter '<head>' '<head>
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,minimum-scale=1.0,user-scalable=no,viewport-fit=cover">
<style>
/* ── Reset ── */
html, body {
  margin: 0; padding: 0;
  width: 100%; height: 100%;
  background: #1a1b26;
  overflow: hidden;
  -webkit-text-size-adjust: 100%;
}
/* ── Make xterm fill the whole screen ── */
#terminal-container,
.xterm,
.xterm-screen,
.xterm-viewport {
  width: 100% !important;
  height: 100% !important;
  max-width: 100% !important;
}
/* Prevent rubber-band scroll on iOS */
body { position: fixed; }
/* ── Keyboard button (bottom-right FAB) ── */
#mob-kb-btn {
  display: none;
  position: fixed;
  bottom: 18px;
  right: 18px;
  z-index: 9999;
  width: 52px;
  height: 52px;
  border-radius: 50%;
  background: #7aa2f7;
  border: none;
  box-shadow: 0 3px 10px rgba(0,0,0,.45);
  cursor: pointer;
  align-items: center;
  justify-content: center;
  font-size: 26px;
  color: #1a1b26;
}
/* Show FAB only on touch devices */
@media (hover: none) and (pointer: coarse) {
  #mob-kb-btn { display: flex; }
}
</style>
<script>
// Add keyboard FAB after DOM is ready
document.addEventListener("DOMContentLoaded", function () {
  var btn = document.createElement("button");
  btn.id = "mob-kb-btn";
  btn.title = "Toggle keyboard";
  btn.textContent = "⌨";
  btn.addEventListener("click", function () {
    // Focus the xterm textarea so the soft keyboard appears
    var ta = document.querySelector(".xterm-helper-textarea");
    if (ta) { ta.focus(); ta.click(); }
    else {
      var canvas = document.querySelector(".xterm-screen canvas");
      if (canvas) canvas.focus();
    }
  });
  document.body.appendChild(btn);
});
</script>';
        sub_filter_once    on;
        sub_filter_types   text/html;
    }

    # ── OpenCode chat UI + REST/WS API ───────────────────────────────
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

# ─── ttyd: real PTY on :7681 ───
# -t fontSize     : readable on mobile (14px default xterm is too small)
# -t lineHeight   : tighter lines = more content visible
# -t cursorBlink  : visual feedback
# -t scrollback   : 2000-line history buffer
# -b /terminal    : base-path so generated JS hits /terminal/ws correctly
# -W              : write mode (allow keyboard input)
echo "[TERMINAL] ttyd on :7681 (base-path /terminal) ..."
nohup ttyd -p 7681 -i 0.0.0.0 \
  -b /terminal \
  -W \
  -t fontSize=15 \
  -t lineHeight=1.1 \
  -t cursorBlink=true \
  -t scrollback=2000 \
  bash -l > /data/logs/ttyd.log 2>&1 &

# ─── OpenCode on :8080 (nginx proxies / → here) ───
echo "[OPENCODE] opencode serve on :8080 ..."
exec opencode serve --port 8080 --hostname 127.0.0.1
