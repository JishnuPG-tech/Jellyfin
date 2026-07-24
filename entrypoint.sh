#!/bin/sh
# Entrypoint for the merged opencode + terminal environment.
#
# Sequence:
#   1. git config + ensure /data/{share,config,cache,state}
#   2. write default opencode config (Zen free model)
#   3. SQLite self-heal daemon
#   4. clone/pull configured project into /projects/default
#   5. background  opencode serve  on :4096
#   6. exec uvicorn                       on :7860  ← THIS IS THE HF PORT
set -u

echo "============================================"
echo "=== OpenCode-Serve starting up            ==="
echo "Time: $(date)"
echo "============================================"

# ── git ownership ──
git config --global --add safe.directory '*' 2>/dev/null || true

echo "[INIT] /data dirs ..."
mkdir -p /data/share/opencode 2>/dev/null || true
mkdir -p /data/config/opencode 2>/dev/null || true
mkdir -p /data/cache/opencode 2>/dev/null || true
mkdir -p /data/state/opencode 2>/dev/null || true

mkdir -p /data/workspaces 2>/dev/null || true
mkdir -p /data/logs 2>/dev/null || true
mkdir -p "$WORKDIR" 2>/dev/null || true

# ── default config ──
python3 - <<'PYEOF' || true
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
if not os.environ.get('ANTHROPIC_API_KEY') and not os.environ.get('OPENAI_API_KEY'):
    d['model'] = 'opencode/big-pickle'
elif not d.get('model'):
    d['model'] = 'opencode/big-pickle'
json.dump(d, open(p, 'w'), indent=2)
print('[CONFIG] model:', d.get('model'), flush=True)
PYEOF

echo "[CONFIG] current:"
cat /data/config/opencode/opencode.json 2>/dev/null || echo "{}"

# ── self-heal ──
echo "[DB] starting SQLite self-heal ..."
python3 /cleaner.py > /data/logs/cleaner.log 2>&1 &
CLEANER_PID=$!

# ── project repo ──
mkdir -p /projects/default
cd /projects/default
GITHUB_REPO="${GITHUB_REPO:-https://github.com/JishnuPG-tech/OpenCode-Drive.git}"

HAS_GIT=$(test -d .git && echo yes || echo no)
HAS_REMOTE=$(git remote -v 2>/dev/null | grep -c origin || echo 0)
IS_EMPTY=$(test -z "$(ls -A . 2>/dev/null | grep -v '^\.')" && echo yes || echo no)
echo "[GIT] state has_git=$HAS_GIT remote=$HAS_REMOTE empty=$IS_EMPTY"

if [ "$HAS_GIT" = "no" ] && [ "$IS_EMPTY" = "yes" ]; then
  echo "[GIT] cloning $GITHUB_REPO ..."
  git clone "$GITHUB_REPO" . 2>&1 | tail -5 || echo "[GIT] clone FAILED"
elif [ "$HAS_GIT" = "yes" ] && [ "$HAS_REMOTE" -gt 0 ]; then
  echo "[GIT] pulling ..."
  git pull origin HEAD 2>/dev/null || echo "[GIT] pull failed"
fi

if [ ! -d /projects/default/.git ]; then
  git init
  git config user.email 'opencode@local.com'
  git config user.name 'OpenCode'
  git commit --allow-empty -m 'Initial commit'
fi

# ── env summary ──
echo "[ENV] ANTHROPIC=$( [ -n "${ANTHROPIC_API_KEY:-}" ] && echo SET || echo NOT_SET)  OPENAI=$( [ -n "${OPENAI_API_KEY:-}" ] && echo SET || echo NOT_SET)"
echo "[ENV] AUTH user=$( [ -n "${OPENCODE_SERVER_USERNAME:-}" ] && echo SET || echo NOT_SET)  pass=$( [ -n "${OPENCODE_SERVER_PASSWORD:-}" ] && echo SET || echo NOT_SET)"

# ── connectivity hint ──
ZEN=$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 https://opencode.ai/zen/v1/models 2>/dev/null || echo 000)
echo "[NET] opencode.ai/zen status: $ZEN"

# ── launch upstream opencode-serve ──
echo "[UPSTREAM] starting opencode serve on :4096 ..."
cd /data/workspaces/default
opencode serve --port 4096 --hostname 127.0.0.1 > /data/logs/opencode-serve.log 2>&1 &
OCPID=$!

# Wait up to 30 s for the upstream
for i in $(seq 1 30); do
  if ! kill -0 $OCPID 2>/dev/null; then
    echo "[UPSTREAM] died early; log follows:"
    cat /data/logs/opencode-serve.log 2>/dev/null | tail -30
    break
  fi
  if curl -sf http://127.0.0.1:4096/global/health >/dev/null 2>&1; then
    echo "[UPSTREAM] ready (attempt $i)"
    break
  fi
  sleep 1
done

# ── launch the Web gateway (FastAPI + embedded PTY) ──
echo "[GATEWAY] starting uvicorn on :${PORT:-7860} ..."
cd /app
exec uvicorn backend.app.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-7860}" \
  --log-level info \
  --no-access-log
