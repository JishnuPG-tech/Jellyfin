#!/bin/sh
# OpenCode-Serve entrypoint — minimal multi-service bootstrap:
#   1. Setup /data
#   2. SQLite self-heal daemon
#   3. opencode serve on 4096 (internal, never exposed)
#   4. ttyd on 7681 (internal, never exposed)
#   5. uvicorn on 7860 (HF's exposed port) — proxies everything
set -u

echo "============================================"
echo "=== OpenCode-Serve starting                ==="
echo "Time: $(date)"
echo "============================================"

# git trust
git config --global --add safe.directory '*' 2>/dev/null || true

# /data dirs (HF persistent bucket)
echo "[INIT] /data dirs ..."
mkdir -p /data/share/opencode /data/config/opencode /data/cache/opencode /data/state/opencode \
         /data/workspaces /data/logs \
 2>/dev/null || true

# Default config: prefer opencode Zen free model
echo "[CONFIG] /data/config ..."
python3 - <<'PYEOF' 2>/dev/null || true
import json, os
p = '/data/config/opencode/opencode.json'
stale = ('big-pickle', 'mimo-v2.5-free', 'opencode/mimo-v2.5-free')
try:
    d = json.load(open(p)) if os.path.exists(p) else {}
    if d.get('model') in stale:
        del d['model']
        json.dump(d, open(p, 'w'), indent=2)
except Exception as e:
    print(f'config normalize error: {e}', flush=True)
try:
    d = json.load(open(p)) if os.path.exists(p) else {}
except Exception:
    d = {}
d['$schema'] = 'https://opencode.ai/config.json'
d['server'] = d.get('server', {'port': 4096, 'hostname': '127.0.0.1'})
if not os.environ.get('ANTHROPIC_API_KEY') and not os.environ.get('OPENAI_API_KEY') and not os.environ.get('OPENCODE_API_KEY'):
    d['model'] = 'opencode/big-pickle'
elif not d.get('model'):
    d['model'] = 'opencode/big-pickle'
json.dump(d, open(p, 'w'), indent=2)
print('[CONFIG] model:', d.get('model'), flush=True)
PYEOF
echo "[CONFIG] final:"
cat /data/config/opencode/opencode.json 2>/dev/null || echo "{}"

# SQLite heal
echo "[DB] starting SQLite self-heal ..."
python3 /cleaner.py > /data/logs/cleaner.log 2>&1 &

# project repo (opt)
mkdir -p /projects/default
cd /projects/default
GITHUB_REPO="${GITHUB_REPO:-https://github.com/JishnuPG-tech/OpenCode-Drive.git}"
if [ ! -d .git ]; then
  if [ -z "$(ls -A . 2>/dev/null | grep -v '^\.')" ]; then
    git clone "$GITHUB_REPO" . 2>&1 | tail -5 || true
  else
    git init && git config user.email 'opencode@local.com' && git config user.name 'OpenCode' && git commit --allow-empty -m 'Initial'
  fi
fi

echo "[ENV] ANTHROPIC=$( [ -n "$ANTHROPIC_API_KEY" ] && echo SET || echo NOT_SET)  OPENAI=$( [ -n "$OPENAI_API_KEY" ] && echo SET || echo NOT_SET)  OPENCODE_API_KEY=$( [ -n "$OPENCODE_API_KEY" ] && echo SET || echo NOT_SET)"
echo "[ENV] AUTH user=$( [ -n "$OPENCODE_SERVER_USERNAME" ] && echo SET || echo NOT_SET)  pass=$( [ -n "$OPENCODE_SERVER_PASSWORD" ] && echo SET || echo NOT_SET)"

# ─── 1) opencode serve on 4096 ───
echo "[UPSTREAM] starting opencode serve on :4096 ..."
mkdir -p /workspace
ln -sfn /data/workspaces/default /workspace/default 2>/dev/null || true
cd /workspace/default 2>/dev/null || cd /projects/default
opencode serve --port 4096 --hostname 127.0.0.1 > /data/logs/opencode-serve.log 2>&1 &
OCPID=$!

# wait for opencode serve up
for i in $(seq 1 60); do
  if curl -sf http://127.0.0.1:4096/global/health >/dev/null 2>&1; then
    echo "[UPSTREAM] opencode serve ready (attempt $i)"
    break
  fi
  sleep 1
done

# ─── 2) ttyd on 7681 ───
echo "[TERMINAL] starting ttyd on :7681 (base /terminal) ..."
ttyd --port 7681 --host 0.0.0.0 --writable --base-path /terminal \
  bash -l > /data/logs/ttyd.log 2>&1 &
TTYD_PID=$!
sleep 1

# ─── 3) uvicorn on 7860 (HF port) ───
echo "[GATEWAY] starting uvicorn on :7860 ..."
cd /app
exec python3 -m uvicorn backend.app.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-7860}" \
  --proxy-headers \
  --no-access-log
