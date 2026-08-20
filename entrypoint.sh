#!/bin/bash

echo "=================================================="
echo " Starting Apex Multi-Project Cloud Suite          "
echo " Hugging Face Space Free Tier (16GB RAM, 2 vCPU)  "
echo "=================================================="

# Ensure runtime directories exist
mkdir -p /tmp/caddy/data /tmp/caddy/config /tmp/stirling-pdf /tmp/stirling-pdf/heap_dumps /tmp/stirling-pdf/libre

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

# Monitor background processes
while kill -0 "$STIRLING_PID" 2>/dev/null && kill -0 "$CADDY_PID" 2>/dev/null; do
    sleep 2
done

echo "[Apex] One of the core services stopped. Cleaning up..."
cleanup
