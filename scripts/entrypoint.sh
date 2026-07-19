#!/usr/bin/env bash
set -euo pipefail

echo "Entrypoint: preparing environment..."

mkdir -p /data /data/workspaces /data/bin /data/logs 2>/dev/null || true

DATA_ROOT="/data"
if [ ! -w /data ]; then
    echo "/data is not writable; falling back to /tmp"
    DATA_ROOT="/tmp"
fi

export PYTHONPATH="/app:${PYTHONPATH:-}"
export WORKSPACE_PATH="${WORKSPACE_PATH:-$DATA_ROOT/workspaces}"
mkdir -p "$WORKSPACE_PATH" "$DATA_ROOT/bin" "$DATA_ROOT/logs" 2>/dev/null || true

# Opencode server auth credentials (override via env vars)
export OPENCODE_SERVER_USERNAME="${OPENCODE_SERVER_USERNAME:-opencode}"
export OPENCODE_SERVER_PASSWORD="${OPENCODE_SERVER_PASSWORD:-password}"

# Write global opencode config so server UI has correct settings
mkdir -p /root/.config/opencode
cat > /root/.config/opencode/opencode.json <<EOF
{
  "\$schema": "https://opencode.ai/config.json",
  "server": {
    "port": 4096,
    "hostname": "127.0.0.1"
  },
  "workspace": "${WORKSPACE_PATH}"
}
EOF

# Ensure opencode is in PATH
if [ ! -f /usr/local/bin/opencode ] && command -v opencode >/dev/null 2>&1; then
    ln -sf "$(command -v opencode)" /usr/local/bin/opencode || true
fi

# Create symlinks in home directories to allow the file explorer to browse workspaces
ln -sf "$WORKSPACE_PATH" /root/workspaces || true
ln -sf "$WORKSPACE_PATH" /home/appuser/workspaces || true

echo "Starting opencode serve on port 4096..."
# Create a default 'projects' placeholder folder so the file explorer isn't empty
mkdir -p "${WORKSPACE_PATH}/projects"
cd "${WORKSPACE_PATH}" || true
opencode serve --port 4096 --hostname 127.0.0.1 >/data/logs/opencode-serve.log 2>&1 &
OPENCODE_PID=$!
echo "opencode serve started (PID: $OPENCODE_PID)"
cd /app || true

# Wait until opencode serve is healthy (up to 30s)
echo "Waiting for opencode serve to be ready..."
for i in $(seq 1 30); do
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:4096/global/health 2>/dev/null || echo "000")
    if [ "$STATUS" = "200" ]; then
        echo "opencode serve is ready (attempt $i)"
        break
    fi
    echo "  [attempt $i] Not ready yet (HTTP $STATUS), retrying..."
    sleep 1
done

echo "Starting uvicorn on port ${PORT:-7860}..."
exec uvicorn backend.app.main:app --host 0.0.0.0 --port "${PORT:-7860}" --log-level info
