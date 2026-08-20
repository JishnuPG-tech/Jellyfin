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
    for pid_var in CADDY_PID STIRLING_PID; do
        pid="${!pid_var:-}"
        [ -n "$pid" ] && kill -TERM "$pid" 2>/dev/null || true
    done
    echo "[Apex] Services stopped."
    exit 0
}

trap cleanup SIGTERM SIGINT

# ── 2. Start Stirling-PDF backend on Port 8080 ────────────────────────────────
echo "[Apex] Starting Stirling-PDF Backend on Port 8080..."
(
  cd /app || cd /stirling-app || cd /
  if [ -f "/app/app.jar" ]; then
    APP_DIR="/app"
  elif [ -f "/stirling-app/app.jar" ]; then
    APP_DIR="/stirling-app"
  else
    APP_DIR="$(find / -name "app.jar" 2>/dev/null | head -n 1 | xargs dirname 2>/dev/null || echo "/app")"
  fi
  
  exec java \
    -Dstirling.base-path=/data/Stirling/ \
    -Dserver.port=8080 \
    -XX:+UseG1GC \
    -XX:MaxGCPauseMillis=200 \
    -Dspring.threads.virtual.enabled=true \
    -Djava.awt.headless=true \
    -cp "${APP_DIR}/app.jar:${APP_DIR}/lib/*" \
    stirling.software.SPDF.SPDFApplication
) &
STIRLING_PID=$!

# ── 3. Start Caddy Gateway on Port 7860 ───────────────────────────────────────
echo "[Apex] Starting Caddy Gateway on Port 7860..."
caddy run --config /etc/caddy/Caddyfile --adapter caddyfile &
CADDY_PID=$!

echo "[Apex] All services online! Gateway listening on port 7860"
echo "[Apex]   Portal Hub    → /"
echo "[Apex]   Stirling-PDF  → /stirling"

# ── 4. Process Supervisor ──────────────────────────────────────────────────────
while true; do
    if ! kill -0 "$CADDY_PID" 2>/dev/null; then
        echo "[Apex] CRITICAL: Caddy gateway exited — shutting down."
        cleanup
    fi

    if ! kill -0 "$STIRLING_PID" 2>/dev/null; then
        echo "[Apex] WARNING: Stirling-PDF exited — restarting..."
        (
          cd /app || cd /stirling-app || cd /
          APP_DIR="$(find / -name "app.jar" 2>/dev/null | head -n 1 | xargs dirname 2>/dev/null || echo "/app")"
          exec java \
            -Dstirling.base-path=/data/Stirling/ \
            -Dserver.port=8080 \
            -XX:+UseG1GC \
            -XX:MaxGCPauseMillis=200 \
            -Dspring.threads.virtual.enabled=true \
            -Djava.awt.headless=true \
            -cp "${APP_DIR}/app.jar:${APP_DIR}/lib/*" \
            stirling.software.SPDF.SPDFApplication
        ) &
        STIRLING_PID=$!
    fi

    sleep 5
done
