#!/bin/bash
set -e

echo "=================================================="
echo " 🚀 Starting Apex Cloud Platform (v2.1 Production)"
echo " Stack: Nginx + Jellyfin + Apex Core + Legacy Tools"
echo "=================================================="

# ── 1. Initialize Storage Layout ───────────────────────────────────────────────
echo "[Apex] Initializing storage directories..."

# Persistent Bucket paths (/data)
mkdir -p /data/Stirling/configs \
         /data/Stirling/logs \
         /data/Stirling/customFiles \
         /data/Stirling/pipeline \
         /data/Stirling/storage \
         /data/Stirling/tessdata \
         /data/jellyfin/config \
         /data/jellyfin/cache \
         /data/jellyfin/media/Movies \
         /data/jellyfin/media/Shows \
         /data/apex/backups \
         /data/apex/session \
         /data/apex/metadata-cache \
         2>/dev/null || true

# High-speed ephemeral paths (/tmp)
mkdir -p /tmp/stirling-pdf \
         /tmp/apex-db \
         /tmp/apex-stream-cache \
         /run/nginx \
         2>/dev/null || true

# Secure session directory permissions
chmod 700 /data/apex/session 2>/dev/null || true
if [ -f "/data/apex/session/session.json" ]; then
    chmod 600 /data/apex/session/session.json 2>/dev/null || true
fi

# ── 2. SQLite Snapshot Restoration ────────────────────────────────────────────
if [ -f "/data/apex/backups/apex_latest.db" ]; then
    echo "[Apex] Restoring SQLite snapshot from /data/apex/backups/apex_latest.db..."
    cp -f /data/apex/backups/apex_latest.db /tmp/apex-db/apex.db
else
    echo "[Apex] No existing database snapshot found. Fresh database will be created at /tmp/apex-db/apex.db."
fi

# Locate Python runtime
PYTHON_BIN="python3"
if [ -f "/opt/venv/bin/python3" ]; then
    PYTHON_BIN="/opt/venv/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
fi

cleanup() {
    echo "[Apex] Received termination signal. Initiating graceful shutdown..."
    
    # Trigger final atomic backup of SQLite before exiting
    if [ -f "/tmp/apex-db/apex.db" ] && command -v sqlite3 >/dev/null 2>&1; then
        echo "[Apex] Performing pre-shutdown SQLite backup..."
        sqlite3 /tmp/apex-db/apex.db "VACUUM INTO '/tmp/apex-db/backup.db';" 2>/dev/null || true
        if [ -f "/tmp/apex-db/backup.db" ]; then
            mv -f /tmp/apex-db/backup.db /data/apex/backups/apex_latest.db
            echo "[Apex] SQLite backup saved to /data/apex/backups/apex_latest.db"
        fi
    fi

    [ -n "${NGINX_PID:-}" ] && kill -TERM "${NGINX_PID}" 2>/dev/null || true
    [ -n "${APEX_CORE_PID:-}" ] && kill -TERM "${APEX_CORE_PID}" 2>/dev/null || true
    [ -n "${JELLYFIN_PID:-}" ] && kill -TERM "${JELLYFIN_PID}" 2>/dev/null || true
    [ -n "${STIRLING_PID:-}" ] && kill -TERM "${STIRLING_PID}" 2>/dev/null || true
    [ -n "${ENHANCER_PID:-}" ] && kill -TERM "${ENHANCER_PID}" 2>/dev/null || true
    
    echo "[Apex] All services stopped."
    exit 0
}

trap cleanup SIGTERM SIGINT

# ── 3. Start Stirling-PDF backend on Port 8080 ────────────────────────────────
echo "[Apex] Locating Stirling-PDF application..."
APP_JAR=""
for candidate in /app/app.jar /app.jar /stirling-app/app.jar; do
    if [ -f "${candidate}" ]; then
        APP_JAR="${candidate}"
        break
    fi
done

if [ -z "${APP_JAR}" ]; then
    APP_JAR="$(find / -name "app.jar" 2>/dev/null | head -n 1)"
fi

if [ -n "${APP_JAR}" ]; then
    echo "[Apex] Found Stirling-PDF jar at: ${APP_JAR}"
    APP_DIR="$(dirname "${APP_JAR}")"
    (
        cd "${APP_DIR}"
        exec java \
            -Dstirling.base-path=/data/Stirling/ \
            -Dserver.port=8080 \
            -XX:+UseG1GC \
            -XX:MaxGCPauseMillis=200 \
            -Dspring.threads.virtual.enabled=true \
            -Djava.awt.headless=true \
            -jar "${APP_JAR}"
    ) &
    STIRLING_PID=$!
fi

# ── 4. Start PDF Enhancer on Port 8082 ────────────────────────────────────────
if [ -d "/opt/pdf_enhancer" ]; then
    echo "[Apex] Starting PDF Enhancer on Port 8082..."
    (
        cd /opt/pdf_enhancer
        exec "${PYTHON_BIN}" -m uvicorn api_server:app \
            --host 0.0.0.0 \
            --port 8082
    ) &
    ENHANCER_PID=$!
fi

# ── 5. Start Jellyfin Media Server on Port 8096 ───────────────────────────────
echo "[Apex] Starting Jellyfin on Port 8096..."
(
    exec /usr/bin/jellyfin \
        --datadir /data/jellyfin/config \
        --cachedir /data/jellyfin/cache \
        --ffmpeg /usr/lib/jellyfin-ffmpeg/ffmpeg \
        --webdir /usr/share/jellyfin/web \
        --restartpath /usr/local/bin/entrypoint.sh
) &
JELLYFIN_PID=$!

# ── 6. Start Apex Go Core Daemon on Port 8084 ─────────────────────────────────
if [ -f "/opt/apex/apex-core" ]; then
    echo "[Apex] Starting Apex Core (Go) on Port 8084..."
    (
        exec /opt/apex/apex-core
    ) &
    APEX_CORE_PID=$!
else
    echo "[Apex] Note: /opt/apex/apex-core not found yet (will start once compiled)."
fi

# ── 7. Start Nginx Ingress Proxy on Port 7860 ─────────────────────────────────
echo "[Apex] Starting Nginx on Port 7860..."
nginx -g "daemon off;" -c /etc/nginx/nginx.conf &
NGINX_PID=$!

echo "[Apex] All services dispatched successfully!"
echo "[Apex] Ingress URL: http://0.0.0.0:7860/"

# ── 8. Process Supervisor & Periodic SQLite Backup Loop ───────────────────────
BACKUP_COUNTER=0

while true; do
    # Check Nginx
    if ! kill -0 "${NGINX_PID}" 2>/dev/null; then
        echo "[Apex] CRITICAL: Nginx gateway exited unexpectedly."
        cleanup
    fi

    # Check Jellyfin
    if ! kill -0 "${JELLYFIN_PID}" 2>/dev/null; then
        echo "[Apex] WARNING: Jellyfin exited — restarting..."
        (
            exec /usr/bin/jellyfin \
                --datadir /data/jellyfin/config \
                --cachedir /data/jellyfin/cache \
                --ffmpeg /usr/lib/jellyfin-ffmpeg/ffmpeg \
                --webdir /usr/share/jellyfin/web \
                --restartpath /usr/local/bin/entrypoint.sh
        ) &
        JELLYFIN_PID=$!
    fi

    # Check Apex Core (if binary exists)
    if [ -f "/opt/apex/apex-core" ]; then
        if [ -z "${APEX_CORE_PID:-}" ] || ! kill -0 "${APEX_CORE_PID}" 2>/dev/null; then
            echo "[Apex] Restarting Apex Core Daemon..."
            (
                exec /opt/apex/apex-core
            ) &
            APEX_CORE_PID=$!
        fi
    fi

    # Periodic SQLite backup (every 180 loops * 5s = 15 minutes)
    BACKUP_COUNTER=$((BACKUP_COUNTER + 1))
    if [ "${BACKUP_COUNTER}" -ge 180 ]; then
        BACKUP_COUNTER=0
        if [ -f "/tmp/apex-db/apex.db" ] && command -v sqlite3 >/dev/null 2>&1; then
            echo "[Apex] Periodic background backup of SQLite database..."
            sqlite3 /tmp/apex-db/apex.db "VACUUM INTO '/tmp/apex-db/backup.db';" 2>/dev/null || true
            if [ -f "/tmp/apex-db/backup.db" ]; then
                mv -f /tmp/apex-db/backup.db /data/apex/backups/apex_latest.db
            fi
        fi
    fi

    sleep 5
done
