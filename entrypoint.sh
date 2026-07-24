#!/bin/sh
# OpenCode-Serve entrypoint — two services, no proxy:
#  * opencode serve on :7860 (HF exposed)  — Chat UI works directly
#  * ttyd on :7681 (internal)  — embedded bash on the same container
set -u

echo "============================================"
echo "=== OpenCode-Serve starting                ==="
echo "Time: $(date)"
echo "============================================"

mkdir -p /data/share/opencode /data/config/opencode /data/cache/opencode /data/state/opencode \
         /data/workspaces /data/logs \
 2>/dev/null || true

# Default config (only when missing)
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

# ─── ttyd on 0.0.0.0:7681 (internal, exposed via nginx-style proxy if needed) ───
echo "[TERMINAL] ttyd on 0.0.0.0:7681 ..."
nohup ttyd --port 7681 --host 0.0.0.0 --writable --base-path /terminal \
  bash -l > /data/logs/ttyd.log 2>&1 &

# ─── opencode serve on 7860 (HF exposed, FG) ───
echo "[UPSTREAM] opencode serve on :${PORT:-7860} ..."
exec opencode serve --port "${PORT:-7860}" --hostname 0.0.0.0
