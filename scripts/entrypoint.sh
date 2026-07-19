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

# Ensure opencode is in PATH
if [ ! -f /usr/local/bin/opencode ] && command -v opencode >/dev/null 2>&1; then
    ln -sf "$(command -v opencode)" /usr/local/bin/opencode || true
fi

echo "Starting uvicorn on port ${PORT:-7860}..."
exec uvicorn backend.app.main:app --host 0.0.0.0 --port "${PORT:-7860}" --log-level info
