#!/bin/bash
set -e

echo "=================================================="
echo " 🚀 Starting Apex Multi-Tool Cloud Suite          "
echo " Hugging Face Space (Persistent Storage Enabled)  "
echo " Included: Stirling-PDF + Gemini Web2API + PDF Enhancer + Caddy "
echo "=================================================="

# ── 1. Initialize persistent storage layout ───────────────────────────────────
echo "[Apex] Initializing persistent storage structure..."
mkdir -p /data/Stirling/configs \
         /data/Stirling/logs \
         /data/Stirling/customFiles \
         /data/Stirling/pipeline \
         /data/Stirling/storage \
         /data/Stirling/tessdata \
         /data/gemini \
         /tmp/stirling-pdf \
         /tmp/caddy/data \
         /tmp/caddy/config \
         2>/dev/null || true

# ── 2. Configure Gemini Web2API config in persistent storage ───────────────────
if [ ! -f "/data/gemini/config.json" ]; then
    echo "[Apex] Initializing default Gemini Web2API configuration in /data/gemini/config.json..."
    cp /etc/gemini_web2api/config.json /data/gemini/config.json 2>/dev/null || true
fi

# If GEMINI_COOKIES env/secret is provided, write to /data/gemini/cookies.json
if [ -n "${GEMINI_COOKIES:-}" ]; then
    echo "[Apex] Configuring Gemini cookies from environment secret..."
    echo "$GEMINI_COOKIES" > /data/gemini/cookies.json
fi

# Locate Python runtime
PYTHON_BIN="python3"
if [ -f "/opt/gemini_venv/bin/python3" ]; then
    PYTHON_BIN="/opt/gemini_venv/bin/python3"
elif [ -f "/opt/venv/bin/python3" ]; then
    PYTHON_BIN="/opt/venv/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
fi

cleanup() {
    echo "[Apex] Received termination signal. Stopping all services..."
    [ -n "${CADDY_PID:-}" ] && kill -TERM "$CADDY_PID" 2>/dev/null || true
    [ -n "${STIRLING_PID:-}" ] && kill -TERM "$STIRLING_PID" 2>/dev/null || true
    [ -n "${GEMINI_PID:-}" ] && kill -TERM "$GEMINI_PID" 2>/dev/null || true
    [ -n "${ENHANCER_PID:-}" ] && kill -TERM "$ENHANCER_PID" 2>/dev/null || true
    echo "[Apex] Services stopped."
    exit 0
}

trap cleanup SIGTERM SIGINT

# ── 3. Locate Stirling-PDF jar ────────────────────────────────────────────────
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

echo "[Apex] Found Stirling-PDF jar at: ${APP_JAR:-/app/app.jar}"
APP_DIR="$(dirname "${APP_JAR:-/app/app.jar}")"

# ── 4. Start Stirling-PDF backend on Port 8080 ────────────────────────────────
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

# ── 5. Start Gemini Web2API backend on Port 8081 ──────────────────────────────
echo "[Apex] Starting Gemini Web2API on Port 8081..."
(
    cd /opt/gemini_web2api
    COOKIE_ARG=()
    if [ -f "/data/gemini/cookies.json" ]; then
        COOKIE_ARG=(--cookie-file "/data/gemini/cookies.json")
    fi
    exec "$PYTHON_BIN" -m gemini_web2api \
        --port 8081 \
        --config "/data/gemini/config.json" \
        "${COOKIE_ARG[@]}"
) &
GEMINI_PID=$!

# ── 6. Start PDF Enhancer (FastAPI + React) on Port 8082 ─────────────────────
echo "[Apex] Starting PDF Enhancer (FastAPI + React 19) on Port 8082..."
(
    cd /opt/pdf_enhancer
    exec "$PYTHON_BIN" -m uvicorn api_server:app \
        --host 0.0.0.0 \
        --port 8082
) &
ENHANCER_PID=$!

# ── 7. Start Caddy Gateway on Port 7860 ───────────────────────────────────────
echo "[Apex] Starting Caddy Gateway on Port 7860..."
caddy run --config /etc/caddy/Caddyfile --adapter caddyfile &
CADDY_PID=$!

echo "[Apex] All services dispatched!"
echo "[Apex]   Portal Hub      → http://0.0.0.0:7860/"
echo "[Apex]   Stirling-PDF    → http://0.0.0.0:7860/stirling"
echo "[Apex]   Gemini Web2API  → http://0.0.0.0:7860/v1"
echo "[Apex]   PDF Enhancer    → http://0.0.0.0:7860/enhancer"

# ── 8. Process Supervisor ──────────────────────────────────────────────────────
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

    if ! kill -0 "$GEMINI_PID" 2>/dev/null; then
        echo "[Apex] WARNING: Gemini Web2API exited — restarting..."
        (
            cd /opt/gemini_web2api
            COOKIE_ARG=()
            if [ -f "/data/gemini/cookies.json" ]; then
                COOKIE_ARG=(--cookie-file "/data/gemini/cookies.json")
            fi
            exec "$PYTHON_BIN" -m gemini_web2api \
                --port 8081 \
                --config "/data/gemini/config.json" \
                "${COOKIE_ARG[@]}"
        ) &
        GEMINI_PID=$!
    fi

    if ! kill -0 "$ENHANCER_PID" 2>/dev/null; then
        echo "[Apex] WARNING: PDF Enhancer exited — restarting..."
        (
            cd /opt/pdf_enhancer
            exec "$PYTHON_BIN" -m uvicorn api_server:app \
                --host 0.0.0.0 \
                --port 8082
        ) &
        ENHANCER_PID=$!
    fi

    sleep 5
done
