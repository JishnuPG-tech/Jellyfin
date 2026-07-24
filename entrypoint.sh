#!/bin/sh
# OpenCode-Serve entrypoint:
#   * opencode serve on :4096 (internal, the AI server with chat UI)
#   * ttyd         on :7681 (internal, the embedded bash terminal)
#   * uvicorn      on :7860 (HF exposed port) — byte-proxy
#
# uvicorn routes:
#   * /terminal/*  ↦ ttyd
#   * everything else ↦ opencode serve
set -u

echo "============================================"
echo "=== OpenCode-Serve starting                ==="
echo "Time: $(date)"
echo "============================================"

mkdir -p /data/share/opencode /data/config/opencode /data/cache/opencode /data/state/opencode \
         /data/workspaces /data/logs \
 2>/dev/null || true

if [ ! -f /data/config/opencode/opencode.json ]; then
  mkdir -p /data/config/opencode
  python3 <<'PYEOF' 2>/dev/null || true
import json
d = {"$schema":"https://opencode.ai/config.json","server":{"port":4096,"hostname":"127.0.0.1"},"model":"opencode/big-pickle"}
json.dump(d, open("/data/config/opencode/opencode.json","w"), indent=2)
PYEOF
fi

mkdir -p /projects/default
cd /projects/default
[ -d .git ] || git init -q 2>/dev/null

# ─── 1) opencode on 127.0.0.1:4096 ───
echo "[UPSTREAM] opencode serve on :4096 ..."
nohup opencode serve --port 4096 --hostname 127.0.0.1 > /data/logs/opencode-serve.log 2>&1 &

# ─── 2) ttyd on :7681 ───
echo "[TERMINAL] ttyd on :7681 ..."
nohup ttyd --port 7681 --host 0.0.0.0 --writable --base-path /terminal \
  bash -l > /data/logs/ttyd.log 2>&1 &

# Brief wait so both bind
sleep 2

# ─── 3) uvicorn on 7860 (HF port) ───
echo "[GATEWAY] uvicorn on :7860 ..."
cd /app
exec python3 -m uvicorn backend.app.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-7860}" \
  --proxy-headers \
  --no-access-log
