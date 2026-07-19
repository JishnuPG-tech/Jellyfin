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

# Opencode server auth credentials (can be overridden via HF Space secrets)
export OPENCODE_SERVER_USERNAME="${OPENCODE_SERVER_USERNAME:-opencode}"
export OPENCODE_SERVER_PASSWORD="${OPENCODE_SERVER_PASSWORD:-password}"

# Ensure opencode is in PATH
if [ ! -f /usr/local/bin/opencode ] && command -v opencode >/dev/null 2>&1; then
    ln -sf "$(command -v opencode)" /usr/local/bin/opencode || true
fi

# Create symlinks in home directories so file explorer can browse workspaces
ln -sf "$WORKSPACE_PATH" /root/workspaces 2>/dev/null || true

# Create a default 'projects' placeholder so file explorer is never empty
mkdir -p "${WORKSPACE_PATH}/projects"

echo "Starting opencode serve on port 4096..."
cd "${WORKSPACE_PATH}" || true
opencode serve --port 4096 --hostname 127.0.0.1 >/data/logs/opencode-serve.log 2>&1 &
OPENCODE_PID=$!
echo "opencode serve started (PID: $OPENCODE_PID)"
cd /app || true

# Wait up to 30s for opencode serve to bind to port 4096
echo "Waiting for opencode serve to be ready..."
READY=0
for i in $(seq 1 30); do
    # Check if process is still alive
    if ! kill -0 "$OPENCODE_PID" 2>/dev/null; then
        echo "  opencode serve process exited early! Check /data/logs/opencode-serve.log"
        cat /data/logs/opencode-serve.log || true
        break
    fi
    # Check if port 4096 is bound (works without curl auth complications)
    if ss -tlnp 2>/dev/null | grep -q ':4096' || \
       netstat -tlnp 2>/dev/null | grep -q ':4096'; then
        echo "opencode serve is ready on port 4096 (attempt $i)"
        READY=1
        break
    fi
    echo "  [attempt $i] Port 4096 not bound yet, retrying..."
    sleep 1
done

if [ "$READY" = "0" ]; then
    echo "WARNING: opencode serve may not be fully ready, starting uvicorn anyway"
    # Give it one extra grace period
    sleep 3
fi

echo "Starting uvicorn on port ${PORT:-7860}..."
exec uvicorn backend.app.main:app --host 0.0.0.0 --port "${PORT:-7860}" --log-level info
