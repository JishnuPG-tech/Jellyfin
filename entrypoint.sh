#!/bin/bash
set -e

echo "=================================================="
echo " 🚀 Starting Apex Multi-Tool Cloud Suite          "
echo " Hugging Face Space (Persistent Storage Enabled)  "
echo " Included: Stirling-PDF + Caddy Gateway + Portal  "
echo "=================================================="

# ── 1. Initialize persistent storage layout ───────────────────────────────────
echo "[Apex] Initializing persistent storage structure..."
mkdir -p /data/Stirling/configs \
         /data/Stirling/logs \
         /data/Stirling/customFiles \
         /data/Stirling/pipeline \
         /data/Stirling/storage \
         /data/Stirling/tessdata \
         /tmp/stirling-pdf \
         /tmp/caddy/data \
         /tmp/caddy/config \
         2>/dev/null || true

cleanup() {
    echo "[Apex] Received termination signal. Stopping services..."
    [ -n "$CADDY_PID" ] && kill -TERM "$CADDY_PID" 2>/dev/null || true
    [ -n "$STIRLING_PID" ] && kill -TERM "$STIRLING_PID" 2>/dev/null || true
    echo "[Apex] Services stopped."
    exit 0
}

trap cleanup SIGTERM SIGINT

# ── 2. Locate Stirling-PDF jar ────────────────────────────────────────────────
echo "[Apex] Locating Stirling-PDF application..."
APP_JAR=""
for candidate in /app/app.jar /app.jar /stirling-app/app.jar; do
    if [ -f "$candidate" ]; then
        APP_JAR="$candidate"
        break
    fi
done

if [ -z "$APP_JAR" ]; then
    APP_JAR="$(find / -name "app.jar" 2>/dev/null | head -n 1)"
fi

echo "[Apex] Found application jar at: ${APP_JAR:-/app/app.jar}"
APP_DIR="$(dirname "${APP_JAR:-/app/app.jar}")"

# ── 3. Start Stirling-PDF backend on Port 8080 ────────────────────────────────
echo "[Apex] Starting Stirling-PDF Backend on Port 8080..."
(
    cd "$APP_DIR"
    exec java \
        -Dstirling.base-path=/data/Stirling/ \
        -Dserver.port=8080 \
        -XX:+UseG1GC \
        -XX:MaxGCPauseMillis=200 \
        -Dspring.threads.virtual.enabled=true \
        -Djava.awt.headless=true \
        -jar "$APP_JAR"
) &
STIRLING_PID=$!

# ── 4. Start Caddy Gateway on Port 7860 ───────────────────────────────────────
echo "[Apex] Starting Caddy Gateway on Port 7860..."
caddy run --config /etc/caddy/Caddyfile --adapter caddyfile &
CADDY_PID=$!

echo "[Apex] All services dispatched!"
echo "[Apex]   Portal Hub    → http://0.0.0.0:7860/"
echo "[Apex]   Stirling-PDF  → http://0.0.0.0:7860/stirling"

# ── 5. Process Supervisor ──────────────────────────────────────────────────────
sleep 3

while true; do
    if ! kill -0 "$CADDY_PID" 2>/dev/null; then
        echo "[Apex] CRITICAL: Caddy gateway exited unexpectedly."
        cleanup
    fi

    if ! kill -0 "$STIRLING_PID" 2>/dev/null; then
        echo "[Apex] WARNING: Stirling-PDF exited — restarting..."
        (
            cd "$APP_DIR"
            exec java \
                -Dstirling.base-path=/data/Stirling/ \
                -Dserver.port=8080 \
                -XX:+UseG1GC \
                -XX:MaxGCPauseMillis=200 \
                -Dspring.threads.virtual.enabled=true \
                -Djava.awt.headless=true \
                -jar "$APP_JAR"
        ) &
        STIRLING_PID=$!
    fi

    sleep 5
done
