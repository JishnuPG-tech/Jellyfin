#!/bin/bash

echo "=================================================="
echo " Starting Apex Multi-Project Cloud Suite          "
echo " Hugging Face Space (Persistent Storage Enabled)  "
echo "=================================================="

# 1. Create persistent storage directories with full read/write permissions
mkdir -p /data/Stirling/configs \
         /data/Stirling/logs \
         /data/Stirling/customFiles \
         /data/Stirling/pipeline \
         /data/Stirling/storage \
         /data/Stirling/tessdata \
         /tmp/caddy/data \
         /tmp/caddy/config \
         /tmp/stirling-pdf \
         /tmp/stirling-pdf/heap_dumps \
         /tmp/stirling-pdf/libre 2>/dev/null || true

chmod -R 777 /data /tmp 2>/dev/null || true

# 2. Point /app working directories directly to /data/Stirling
rm -rf /app/configs /app/logs /app/customFiles /app/pipeline /app/storage 2>/dev/null || true
ln -sfn /data/Stirling/configs /app/configs
ln -sfn /data/Stirling/logs /app/logs
ln -sfn /data/Stirling/customFiles /app/customFiles
ln -sfn /data/Stirling/pipeline /app/pipeline
ln -sfn /data/Stirling/storage /app/storage

# 3. Load persistent Tesseract OCR language models if present
if [ -d "/data/Stirling/tessdata" ] && [ "$(ls -A /data/Stirling/tessdata 2>/dev/null)" ]; then
    echo "[Apex] Loading persistent Tesseract OCR language models..."
    cp -rn /data/Stirling/tessdata/* /usr/share/tesseract-ocr/5/tessdata/ 2>/dev/null || true
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

# 4. Start Caddy Gateway on port 7860
echo "[Apex] Starting Caddy Gateway on Port 7860..."
caddy run --config /app/Caddyfile --adapter caddyfile &
CADDY_PID=$!

# 5. Start Stirling-PDF on port 8080
echo "[Apex] Starting Stirling-PDF Backend..."
/scripts/init.sh &
STIRLING_PID=$!

# Monitor processes
while kill -0 "$STIRLING_PID" 2>/dev/null && kill -0 "$CADDY_PID" 2>/dev/null; do
    sleep 2
done

echo "[Apex] Core service stopped. Cleaning up..."
cleanup
