#!/bin/bash
set -e

echo "=================================================="
echo " 🚀 Starting Apex Cloud Platform (v2.1 Production)"
echo " Stack: Caddy Gateway + Jellyfin + Apex Go Core"
echo "=================================================="

# ── 1. Initialize Storage Layout ───────────────────────────────────────────────
echo "[Apex] Initializing storage directories..."

mkdir -p /data/Stirling/configs \
         /data/Stirling/logs \
         /data/Stirling/customFiles \
         /data/Stirling/pipeline \
         /data/Stirling/storage \
         /data/Stirling/tessdata \
         /data/jellyfin/data \
         /data/jellyfin/config \
         /data/jellyfin/backups \
         /data/jellyfin/log \
         /data/jellyfin/media/Movies \
         /data/jellyfin/media/Shows \
         /data/jellyfin/.aspnet/DataProtection-Keys \
         /data/apex/backups \
         /data/apex/session \
         /data/apex/metadata-cache \
         /tmp/stirling-pdf \
         /tmp/jellyfin-cache \
         /tmp/caddy/data \
         /tmp/caddy/config \
         /tmp/apex-db \
         /tmp/apex-stream-cache \
         2>/dev/null || true

# Permanently persist ASP.NET Core DataProtection keys in /data
mkdir -p /data/jellyfin/.aspnet/DataProtection-Keys
rm -rf /root/.aspnet 2>/dev/null || true
ln -sfn /data/jellyfin/.aspnet /root/.aspnet
export DOTNET_CLI_HOME="/data/jellyfin"

# Secure session directory permissions
chmod 700 /data/apex/session 2>/dev/null || true
if [ -f "/data/apex/session/session.json" ]; then
    chmod 600 /data/apex/session/session.json 2>/dev/null || true
fi

# Locate Python runtime
PYTHON_BIN="python3"
if [ -f "/opt/venv/bin/python3" ]; then
    PYTHON_BIN="/opt/venv/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
fi

# ── 2. SQLite Snapshot Restoration ────────────────────────────────────────────
if [ -f "/data/apex/backups/apex_latest.db" ]; then
    echo "[Apex] Restoring SQLite snapshot from /data/apex/backups/apex_latest.db..."
    cp -f /data/apex/backups/apex_latest.db /tmp/apex-db/apex.db
else
    echo "[Apex] No existing database snapshot found. Fresh database will be created at /tmp/apex-db/apex.db."
fi

# ── 3. Jellyfin Clean Start & Authentication Reset ───────────────────────────
if [ ! -f "/data/jellyfin/.fresh_start_done" ] || [ "${RESET_JELLYFIN_AUTH:-}" = "true" ]; then
    echo "[Apex] User requested fresh start: wiping old Jellyfin credentials and configuration..."
    rm -rf /data/jellyfin/data/* \
           /data/jellyfin/config/* \
           /data/jellyfin/backups/* \
           /data/jellyfin/.aspnet/* \
           /data/jellyfin/.auth_* 2>/dev/null || true
    mkdir -p /data/jellyfin/data \
             /data/jellyfin/config \
             /data/jellyfin/backups \
             /data/jellyfin/.aspnet/DataProtection-Keys
    touch /data/jellyfin/.fresh_start_done
    echo "[Apex] Fresh start complete. Jellyfin Initial Setup Wizard will be presented."
fi

# Pre-flight check: ensure no failed-login account lockouts if database exists
repair_jellyfin_auth() {
    ${PYTHON_BIN} -c "
import os, sqlite3

db_path = '/data/jellyfin/data/data/jellyfin.db'
if os.path.exists(db_path):
    try:
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        cur.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name='Users';\")
        if cur.fetchone():
            cur.execute('UPDATE Users SET InvalidLoginAttemptCount = 0;')
            con.commit()
        con.close()
    except Exception as e:
        print(f'[Apex] Account unlock check notice: {e}')
" 2>/dev/null || true
}

repair_jellyfin_auth

# ── 4. Periodic & Shutdown SQLite Snapshot Handlers ───────────────────────────
backup_sqlite() {
    if [ -f "/tmp/apex-db/apex.db" ]; then
        ${PYTHON_BIN} -c "
import sqlite3, os, shutil
try:
    con = sqlite3.connect('/tmp/apex-db/apex.db')
    con.execute(\"VACUUM INTO '/tmp/apex-db/backup.db'\")
    con.close()
    if os.path.exists('/tmp/apex-db/backup.db'):
        shutil.move('/tmp/apex-db/backup.db', '/data/apex/backups/apex_latest.db')
        print('[Apex] Apex Core SQLite snapshot successfully saved.')
except Exception as e:
    print(f'[Apex] Apex SQLite backup warning: {e}')
" 2>/dev/null || true
    fi
}

backup_jellyfin_sqlite() {
    if [ -f "/data/jellyfin/data/data/jellyfin.db" ]; then
        ${PYTHON_BIN} -c "
import sqlite3, os, shutil
try:
    con = sqlite3.connect('/data/jellyfin/data/data/jellyfin.db')
    # Flush all WAL journal pages into the primary database file
    con.execute('PRAGMA wal_checkpoint(TRUNCATE);')
    con.execute(\"VACUUM INTO '/tmp/jellyfin_backup.db'\")
    con.close()
    if os.path.exists('/tmp/jellyfin_backup.db'):
        shutil.move('/tmp/jellyfin_backup.db', '/data/jellyfin/backups/jellyfin_latest.db')
        print('[Apex] Jellyfin SQLite snapshot successfully saved to /data/jellyfin/backups/jellyfin_latest.db.')
except Exception as e:
    print(f'[Apex] Jellyfin SQLite backup notice: {e}')
" 2>/dev/null || true
    fi
}

cleanup() {
    echo "[Apex] Received termination signal. Initiating graceful shutdown..."
    
    # 1. Allow Jellyfin to flush its state and close database connections cleanly
    if [ -n "${JELLYFIN_PID:-}" ]; then
        kill -TERM "${JELLYFIN_PID}" 2>/dev/null || true
        for i in {1..10}; do
            kill -0 "${JELLYFIN_PID}" 2>/dev/null || break
            sleep 0.5
        done
    fi

    # 2. Checkpoint and backup all SQLite databases to persistent /data
    backup_sqlite
    backup_jellyfin_sqlite

    # 3. Stop remaining daemons
    [ -n "${CADDY_PID:-}" ] && kill -TERM "${CADDY_PID}" 2>/dev/null || true
    [ -n "${APEX_CORE_PID:-}" ] && kill -TERM "${APEX_CORE_PID}" 2>/dev/null || true
    [ -n "${STIRLING_PID:-}" ] && kill -TERM "${STIRLING_PID}" 2>/dev/null || true
    [ -n "${ENHANCER_PID:-}" ] && kill -TERM "${ENHANCER_PID}" 2>/dev/null || true
    
    echo "[Apex] All services stopped cleanly. Persistent state saved to /data."
    exit 0
}

trap cleanup SIGTERM SIGINT

# ── 5. Start Stirling-PDF backend on Port 8080 ────────────────────────────────
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

# ── 6. Start PDF Enhancer on Port 8082 ────────────────────────────────────────
UVICORN_BIN=""
if [ -f "/opt/venv/bin/uvicorn" ]; then
    UVICORN_BIN="/opt/venv/bin/uvicorn"
elif command -v uvicorn >/dev/null 2>&1; then
    UVICORN_BIN="$(command -v uvicorn)"
fi

if [ -d "/opt/pdf_enhancer" ] && [ -n "${UVICORN_BIN}" ]; then
    echo "[Apex] Starting PDF Enhancer on Port 8082..."
    (
        cd /opt/pdf_enhancer
        exec "${UVICORN_BIN}" api_server:app \
            --host 0.0.0.0 \
            --port 8082
    ) &
    ENHANCER_PID=$!
elif [ -d "/opt/pdf_enhancer" ]; then
    echo "[Apex] Notice: uvicorn not available. Skipping PDF Enhancer startup."
fi

# ── 7. Start Jellyfin Media Server on Port 8096 ───────────────────────────────
JELLYFIN_BIN="/opt/jellyfin/jellyfin"
if [ ! -f "${JELLYFIN_BIN}" ] && [ -f "/usr/bin/jellyfin" ]; then
    JELLYFIN_BIN="/usr/bin/jellyfin"
fi

if [ -f "${JELLYFIN_BIN}" ]; then
    echo "[Apex] Locating verified FFmpeg binary..."
    FFMPEG_PATH="/usr/lib/jellyfin-ffmpeg/ffmpeg"
    if [ ! -x "${FFMPEG_PATH}" ] || ! "${FFMPEG_PATH}" -version >/dev/null 2>&1; then
        if [ -x "/usr/local/bin/ffmpeg" ] && /usr/local/bin/ffmpeg -version >/dev/null 2>&1; then
            FFMPEG_PATH="/usr/local/bin/ffmpeg"
        elif command -v ffmpeg >/dev/null 2>&1 && ffmpeg -version >/dev/null 2>&1; then
            FFMPEG_PATH="$(command -v ffmpeg)"
        fi
    fi

    WEBDIR="/opt/jellyfin/jellyfin-web"
    [ ! -d "${WEBDIR}" ] && WEBDIR="/usr/share/jellyfin/web"

    echo "[Apex] Starting Jellyfin on Port 8096 (FFmpeg: ${FFMPEG_PATH}, Web: ${WEBDIR})..."
    (
        exec "${JELLYFIN_BIN}" \
            -d /data/jellyfin/data \
            -c /data/jellyfin/config \
            -C /tmp/jellyfin-cache \
            -l /data/jellyfin/log \
            --ffmpeg "${FFMPEG_PATH}" \
            --webdir "${WEBDIR}"
    ) &
    JELLYFIN_PID=$!
else
    echo "[Apex] Notice: Jellyfin binary not found. Skipping Jellyfin startup."
fi

# ── 8. Start Apex Go Core Daemon on Port 8084 ─────────────────────────────────
if [ -f "/opt/apex/apex-core" ]; then
    echo "[Apex] Starting Apex Core (Go) on Port 8084..."
    (
        exec /opt/apex/apex-core
    ) &
    APEX_CORE_PID=$!
fi

# ── 9. Start Caddy Gateway on Port 7860 ───────────────────────────────────────
echo "[Apex] Starting Caddy Gateway on Port 7860..."
caddy fmt --overwrite /etc/caddy/Caddyfile 2>/dev/null || true
caddy run --config /etc/caddy/Caddyfile --adapter caddyfile &
CADDY_PID=$!

echo "[Apex] All services dispatched successfully!"
echo "[Apex] Ingress URL: http://0.0.0.0:7860/"

# ── 10. Process Supervisor & Periodic Snapshot Backup Loop ────────────────────
BACKUP_COUNTER=0

while true; do
    # Check Caddy
    if ! kill -0 "${CADDY_PID}" 2>/dev/null; then
        echo "[Apex] CRITICAL: Caddy gateway exited unexpectedly."
        cleanup
    fi

    # Check Jellyfin (if running)
    if [ -n "${JELLYFIN_PID:-}" ] && ! kill -0 "${JELLYFIN_PID}" 2>/dev/null; then
        echo "[Apex] WARNING: Jellyfin exited — restarting..."
        (
            exec "${JELLYFIN_BIN}" \
                -d /data/jellyfin/data \
                -c /data/jellyfin/config \
                -C /tmp/jellyfin-cache \
                -l /data/jellyfin/log \
                --ffmpeg "${FFMPEG_PATH}" \
                --webdir "${WEBDIR}"
        ) &
        JELLYFIN_PID=$!
    fi

    # Check Apex Core (if running)
    if [ -f "/opt/apex/apex-core" ]; then
        if [ -z "${APEX_CORE_PID:-}" ] || ! kill -0 "${APEX_CORE_PID}" 2>/dev/null; then
            echo "[Apex] Restarting Apex Core Daemon..."
            (
                exec /opt/apex/apex-core
            ) &
            APEX_CORE_PID=$!
        fi
    fi

    # Periodic SQLite snapshots for Apex Core and Jellyfin (every 180 loops * 5s = 15 minutes)
    BACKUP_COUNTER=$((BACKUP_COUNTER + 1))
    if [ "${BACKUP_COUNTER}" -ge 180 ]; then
        BACKUP_COUNTER=0
        backup_sqlite
        backup_jellyfin_sqlite
    fi

    sleep 5
done
