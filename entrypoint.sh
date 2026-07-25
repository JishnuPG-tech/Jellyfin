#!/bin/sh
# OpenCode-Serve entrypoint
# Runs:
#  1. ttyd on :7681 (internal) — embedded terminal
#  2. opencode serve on :4096  — upstream (internal, no direct access)
#  3. uvicorn gateway on :7860 — the only HF-exposed port
set -u

echo "============================================"
echo "=== OpenCode-Serve starting (proxy mode) ==="
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

echo "[TERMINAL] ttyd on 0.0.0.0:7681 ..."
nohup ttyd -p 7681 -i 0.0.0.0 -W \
  bash -l > /data/logs/ttyd.log 2>&1 &

echo "[UPSTREAM] opencode serve on :${OPENCODE_PORT:-4096} ..."
nohup opencode serve --port "${OPENCODE_PORT:-4096}" --hostname 0.0.0.0 > /data/logs/opencode.log 2>&1 &

echo "[GATEWAY] uvicorn on :${PORT:-7860} ..."
export PYTHONPATH=/app
exec python3 -m uvicorn backend.app.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-7860}" \
  --log-level warning \
  --limit-concurrency 100 \
  --timeout-keep-alive 600
