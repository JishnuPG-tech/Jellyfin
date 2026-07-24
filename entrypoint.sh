#!/bin/sh
# OpenCode-Serve entrypoint:
#   * opencode serve on :4096 (internal, the AI server with chat UI)
#   * ttyd         on :7681 (internal, the embedded terminal)
#   * uvicorn      on :7860 (HF exposed port)
#
# uvicorn ONLY routes specific prefixes; everything else falls back to
# opencode-on-7860 directly... no wait, that requires two services on
# the same port. Instead:
#   * uvicorn proxies /terminal/* to ttyd
#   * uvicorn proxies everything else to opencode serve
set -u

echo "============================================"
echo "=== OpenCode-Serve starting                ==="
echo "Time: $(date)"
echo "============================================"

mkdir -p /data/share/opencode /data/config/opencode /data/cache/opencode /data/state/opencode \
         /data/workspaces /data/logs \
 2>/dev/null || true

# Default config
if [ ! -f /data/config/opencode/opencode.json ]; then
  mkdir -p /data/config/opencode
  python3 -c '
import json
d = {"$schema":"https://opencode.ai/config.json","server":{"port":4096,"hostname":"127.0.0.1"},"model":"opencode/big-pickle"}
json.dump(d, open("/data/config/opencode/opencode.json","w"), indent=2)
' 2>/dev/null || true
fi

mkdir -p /projects/default
cd /projects/default
if [ ! -d .git ]; then
  git init -q 2>/dev/null
  git config user.email 'opencode@local.com' 2>/dev/null
  git config user.name 'OpenCode' 2>/dev/null
  git commit --allow-empty -q -m 'Init' 2>/dev/null || true
fi

# ─── 1) opencode serve on 127.0.0.1:4096 ───
echo "[UPSTREAM] starting opencode serve on :4096 ..."
nohup opencode serve --port 4096 --hostname 127.0.0.1 > /data/logs/opencode-serve.log 2>&1 &

# Wait briefly (don't block forever)
for i in 1 2 3 4 5; do
  curl -sf http://127.0.0.1:4096/global/health >/dev/null 2>&1 && break
  sleep 1
done

# ─── 2) ttyd on 0.0.0.0:7681 ───
echo "[TERMINAL] starting ttyd on :7681 (base /terminal) ..."
nohup ttyd --port 7681 --host 0.0.0.0 --writable --base-path /terminal \
  bash -l > /data/logs/ttyd.log 2>&1 &

# Brief wait so ttyd binds
sleep 1

# ─── 3) uvicorn on 7860 (HF port, foreground) ───
echo "[GATEWAY] starting uvicorn on :7860 ..."
cd /app
exec python3 -m uvicorn backend.app.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-7860}" \
  --proxy-headers \
  --log-level info
