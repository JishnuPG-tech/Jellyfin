#!/bin/bash

echo "=================================================="
echo " Starting Apex Multi-Project Cloud Suite          "
echo " Hugging Face Space (Persistent Storage Enabled)  "
echo "=================================================="

# Define persistent storage location
PERSISTENT_ROOT="/data/Stirling"

# Ensure all persistent directories exist
mkdir -p "${PERSISTENT_ROOT}/configs" \
         "${PERSISTENT_ROOT}/logs" \
         "${PERSISTENT_ROOT}/customFiles" \
         "${PERSISTENT_ROOT}/pipeline" \
         "${PERSISTENT_ROOT}/storage" \
         "${PERSISTENT_ROOT}/tessdata" \
         /tmp/caddy/data \
         /tmp/caddy/config \
         /tmp/stirling-pdf \
         /tmp/stirling-pdf/heap_dumps \
         /tmp/stirling-pdf/libre 2>/dev/null || true

# Map Stirling-PDF system directories to persistent storage
for dir in configs logs customFiles pipeline storage; do
    target="${PERSISTENT_ROOT}/${dir}"
    if [ -d "/${dir}" ] && [ ! -L "/${dir}" ]; then
        # Migrate initial files if persistent directory is fresh
        if [ -z "$(ls -A "${target}" 2>/dev/null)" ]; then
            cp -rn "/${dir}"/* "${target}"/ 2>/dev/null || true
        fi
        rm -rf "/${dir}"
    fi
    ln -sfn "${target}" "/${dir}" 2>/dev/null || true
done

# Sync persistent OCR tessdata language models if present
if [ -d "${PERSISTENT_ROOT}/tessdata" ] && [ "$(ls -A "${PERSISTENT_ROOT}/tessdata" 2>/dev/null)" ]; then
    echo "[Apex] Loading persistent Tesseract OCR language data..."
    cp -rn "${PERSISTENT_ROOT}/tessdata"/* /usr/share/tesseract-ocr/5/tessdata/ 2>/dev/null || true
fi

cleanup() {
    echo "[Apex] Received termination signal. Stopping all services..."
    if [ -n "${CADDY_PID:-}" ] && kill -0 "$CADDY_PID" 2>/dev/null; then
        kill -TERM "$CADDY_PID" 2>/dev/null || true
    fi
    if [ -n "${STIRLING_PID:-}" ] && kill -0 "$STIRLING_PID" 2>/dev/null; then
        kill -TERM "$STIRLING_PID" 2>/dev/null || true
    fi
    wait "${STIRLING_PID:-}" 2>/dev/null || true
    wait "${CADDY_PID:-}" 2>/dev/null || true
    echo "[Apex] Shutdown complete."
    exit 0
}

trap cleanup SIGTERM SIGINT

# 1. Start Caddy Gateway on port 7860
echo "[Apex] Starting Caddy Gateway on Port 7860..."
caddy run --config /app/Caddyfile --adapter caddyfile &
CADDY_PID=$!

# 2. Start Stirling-PDF on port 8080
echo "[Apex] Starting Stirling-PDF Backend..."
/scripts/init.sh &
STIRLING_PID=$!

# Monitor processes
while kill -0 "$STIRLING_PID" 2>/dev/null && kill -0 "$CADDY_PID" 2>/dev/null; do
    sleep 2
done

echo "[Apex] One of the core services stopped. Cleaning up..."
cleanup
