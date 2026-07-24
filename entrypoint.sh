#!/bin/sh
# OpenCode-Serve entrypoint — minimal multi-service bootstrap.
# No git clone, no extra setup. Just:
#   1. /data dirs
#   2. opencode serve on 127.0.0.1:4096
#   3. ttyd on 0.0.0.0:7681 (--base-path /terminal)
#   4. uvicorn on 0.0.0.0:7860 (HF exposed port)
set -u

echo "============================================"
echo "=== OpenCode-Serve starting                ==="
echo "Time: $(date)"
echo "============================================"

mkdir -p /data/share/opencode /data/config/opencode /data/cache/opencode /data/state/opencode \
         /data/workspaces /data/logs \
 2>/dev/null || true

if [ ! -f /data/config/opencode/opencode.json ]; then
  python3 -c '
import json
d = {"$schema":"https://opencode.ai/config.json","server":{"port":4096,"hostname":"127.0.0.1"},"model":"opencode/big-pickle"}
json.dump(d, open("/data/config/opencode/opencode.json","w"), indent=2)
print("[CONFIG] wrote default config")
'
fi

# /projects/default (needed for opencode's "default" project)
mkdir -p /projects/default
if [ ! -d /projects/default/.git ]; then
  (cd /projects/default && git init -q && git config user.email 'opencode@local.com' && git config user.name 'OpenCode' && git commit --allow-empty -q -m 'Init') || true
fi

# ─── 1) opencode serve on 127.0.0.1:4096 ───
echo "[UPSTREAM] starting opencode serve on :4096 ..."
cd /projects/default
nohup opencode serve --port 4096 --hostname 127.0.0.1 > /data/logs/opencode-serve.log 2>&1 &
OCPID=$!
echo "[UPSTREAM] pid=$OCPID"

# ─── 2) ttyd on 0.0.0.0:7681 ───
echo "[TERMINAL] starting ttyd on :7681 (base /terminal) ..."
nohup ttyd --port 7681 --host 0.0.0.0 --writable --base-path /terminal \
  bash -l > /data/logs/ttyd.log 2>&1 &
TTYD_PID=$!
echo "[TERMINAL] pid=$TTYD_PID"

# ─── 3) uvicorn on 7860 (HF port, foreground) ───
echo "[GATEWAY] starting uvicorn on :7860 ..."
cd /app
exec python3 -m uvicorn backend.app.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-7860}" \
  --proxy-headers \
  --no-access-log \
  --log-level info
