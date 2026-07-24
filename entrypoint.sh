#!/bin/sh
# ULTRA-MINIMAL entrypoint — just run opencode serve on 7860 directly.
set -u

echo "============================================"
echo "=== OpenCode-Serve starting                ==="
echo "Time: $(date)"
echo "============================================"

# /data dirs
mkdir -p /data/share/opencode /data/config/opencode /data/cache/opencode /data/state/opencode \
         /data/workspaces /data/logs \
 2>/dev/null || true

# Default config (idempotent)
if [ ! -f /data/config/opencode/opencode.json ]; then
  mkdir -p /data/config/opencode
fi
python3 <<'PYEOF' 2>/dev/null || true
import json, os
p = '/data/config/opencode/opencode.json'
try:
    d = json.load(open(p)) if os.path.exists(p) else {}
except Exception:
    d = {}
d['$schema'] = 'https://opencode.ai/config.json'
d['server'] = d.get('server', {'port': 4096, 'hostname': '0.0.0.0'})
providers = d.get('provider') or {}
d.pop('provider', None)
if not d.get('model'):
    d['model'] = 'opencode/big-pickle'
os.makedirs(os.path.dirname(p), exist_ok=True)
json.dump(d, open(p, 'w'), indent=2)
PYEOF

# /projects with default git
mkdir -p /projects/default
cd /projects/default
[ -d .git ] || git init -q 2>/dev/null

# Start ttyd on a secondary port (internal; HF won't expose it but it's
# useful for SSH access and debugging).
echo "[TERMINAL] starting ttyd on 7681 ..."
nohup ttyd --port 7681 --host 0.0.0.0 --writable --base-path /terminal \
  bash -l > /data/logs/ttyd.log 2>&1 &

# Run opencode serve on 7860 (HF exposed port) — directly, no proxy.
echo "[UPSTREAM] starting opencode serve on :${PORT:-7860} ..."
exec opencode serve --port "${PORT:-7860}" --hostname 0.0.0.0 2>&1 | tee /data/logs/opencode-serve.log
