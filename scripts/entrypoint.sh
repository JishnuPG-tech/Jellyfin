#!/usr/bin/env bash
set -euo pipefail

echo "Entrypoint: preparing environment..."

mkdir -p /data /data/workspaces /data/bin /data/logs 2>/dev/null || true

DATA_ROOT="/data"
if [ ! -w /data ]; then
    echo "/data is not writable; falling back to /tmp"
    DATA_ROOT="/tmp"
    mkdir -p "$DATA_ROOT/workspaces" "$DATA_ROOT/bin" "$DATA_ROOT/logs" 2>/dev/null || true
fi

export PYTHONPATH="/app:${PYTHONPATH:-}"
export WORKSPACE_PATH="${WORKSPACE_PATH:-$DATA_ROOT/workspaces}"
mkdir -p "$WORKSPACE_PATH" "$DATA_ROOT/bin" "$DATA_ROOT/logs" 2>/dev/null || true

# ── Set HOME to /data so opencode's folder picker defaults to /data/workspaces ──
export HOME="/data"
mkdir -p /data/.config /data/.local 2>/dev/null || true

# Ensure opencode is in PATH
if ! command -v opencode >/dev/null 2>&1; then
    echo "ERROR: opencode binary not found!"
    exit 1
fi
if [ ! -f /usr/local/bin/opencode ] && command -v opencode >/dev/null 2>&1; then
    ln -sf "$(command -v opencode)" /usr/local/bin/opencode || true
fi

# ── Pre-populate workspace folders so the file picker always has content ──
mkdir -p "${WORKSPACE_PATH}/projects"
mkdir -p "${WORKSPACE_PATH}/my-code"
mkdir -p "${WORKSPACE_PATH}/scratch"

# Create README in each so they're obviously navigable
for dir in "${WORKSPACE_PATH}/projects" "${WORKSPACE_PATH}/my-code" "${WORKSPACE_PATH}/scratch"; do
    if [ ! -f "$dir/README.md" ]; then
        echo "# $(basename $dir)" > "$dir/README.md"
        echo "Workspace folder on cloud server." >> "$dir/README.md"
    fi
done

# Make workspaces directly visible from HOME (/data)
# opencode's find/file will search $HOME by default
ln -sf "$WORKSPACE_PATH" /data/workspaces 2>/dev/null || true

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
        echo "  opencode serve process exited! Log:"
        cat /data/logs/opencode-serve.log 2>/dev/null || true
        break
    fi
    # Check if port 4096 is bound
    if ss -tlnp 2>/dev/null | grep -q ':4096'; then
        echo "opencode serve is ready on port 4096 (attempt $i)"
        READY=1
        break
    fi
    echo "  [attempt $i] Port 4096 not bound yet..."
    sleep 1
done

if [ "$READY" = "0" ]; then
    echo "WARNING: opencode serve not ready after 30s. Starting uvicorn anyway."
    sleep 2
fi

echo "Starting uvicorn on port ${PORT:-7860}..."
exec uvicorn backend.app.main:app --host 0.0.0.0 --port "${PORT:-7860}" --log-level info
