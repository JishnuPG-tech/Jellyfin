#!/bin/bash
set -e

echo "=================================================="
echo " 🚀 Starting Apex Cloud Platform (v2.0 Production)"
echo " Stack: Nginx Gateway + Jellyfin + Apex Go Core"
echo "=================================================="

# ── 1. Storage Layout Initialization ───────────────────────────────────────────
echo "[Apex] Initializing storage directories..."

mkdir -p /data/jellyfin/data \
         /data/jellyfin/config \
         /data/jellyfin/backups \
         /data/jellyfin/log \
         /data/jellyfin/media/Movies \
         /data/jellyfin/media/Shows \
         /data/jellyfin/.aspnet/DataProtection-Keys \
         /data/apex/backups \
         /data/apex/session \
         /data/apex/metadata-cache \
         /tmp/jellyfin-cache \
         /tmp/apex-db \
         /tmp/apex-stream-cache \
         2>/dev/null || true

# Permanently persist ASP.NET Core DataProtection keys in /data
mkdir -p /data/jellyfin/.aspnet/DataProtection-Keys
rm -rf /root/.aspnet 2>/dev/null || true
ln -sfn /data/jellyfin/.aspnet /root/.aspnet
export DOTNET_CLI_HOME="/data/jellyfin"

# Secure session directory
chmod 700 /data/apex/session 2>/dev/null || true
if [ -f "/data/apex/session/session.json" ]; then
    chmod 600 /data/apex/session/session.json 2>/dev/null || true
fi

# ── 2. Mandatory Secret Key Validation ─────────────────────────────────────────
if [ -z "${APEX_SECRET_KEY:-}" ]; then
    echo "[Apex] FATAL: APEX_SECRET_KEY environment variable is required but missing."
    echo "[Apex] Please configure APEX_SECRET_KEY in Hugging Face Space Secrets or container environment."
    exit 1
fi
echo "[Apex] APEX_SECRET_KEY verified."

# ── 3. SQLite Snapshot Restoration for Apex Core ──────────────────────────────
if [ -f "/data/apex/backups/apex_latest.db" ]; then
    echo "[Apex] Restoring Apex Core SQLite snapshot from /data/apex/backups/apex_latest.db..."
    cp -f /data/apex/backups/apex_latest.db /tmp/apex-db/apex.db
else
    echo "[Apex] Fresh database initialized at /tmp/apex-db/apex.db."
fi

# ── 4. Pre-configure Jellyfin Library Structure ────────────────────────────────
mkdir -p /data/jellyfin/data/root/default/Movies \
         /data/jellyfin/data/root/default/Shows

if [ ! -f "/data/jellyfin/data/root/default/Movies/options.xml" ]; then
    cat << 'EOF' > /data/jellyfin/data/root/default/Movies/options.xml
<?xml version="1.0" encoding="utf-8"?>
<LibraryOptions xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <Enabled>true</Enabled>
  <EnablePhotos>false</EnablePhotos>
  <EnableRealtimeMonitor>true</EnableRealtimeMonitor>
  <PathInfos>
    <MediaPathInfo>
      <Path>/data/jellyfin/media/Movies</Path>
    </MediaPathInfo>
  </PathInfos>
</LibraryOptions>
EOF
fi

if [ ! -f "/data/jellyfin/data/root/default/Shows/options.xml" ]; then
    cat << 'EOF' > /data/jellyfin/data/root/default/Shows/options.xml
<?xml version="1.0" encoding="utf-8"?>
<LibraryOptions xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <Enabled>true</Enabled>
  <EnablePhotos>false</EnablePhotos>
  <EnableRealtimeMonitor>true</EnableRealtimeMonitor>
  <PathInfos>
    <MediaPathInfo>
      <Path>/data/jellyfin/media/Shows</Path>
    </MediaPathInfo>
  </PathInfos>
</LibraryOptions>
EOF
fi

# ── 5. Graceful Shutdown & Snapshot Backup Handlers ────────────────────────────
backup_apex_sqlite() {
    if [ -f "/tmp/apex-db/apex.db" ]; then
        cp -f /tmp/apex-db/apex.db /tmp/apex-db/apex_backup.db 2>/dev/null || true
        if [ -f "/tmp/apex-db/apex_backup.db" ]; then
            mv -f /tmp/apex-db/apex_backup.db /data/apex/backups/apex_latest.db
            echo "[Apex] Apex Core SQLite snapshot saved to /data/apex/backups/apex_latest.db."
        fi
    fi
}

cleanup() {
    echo "[Apex] Received shutdown signal. Commencing clean termination..."

    # 1. Gracefully stop Jellyfin so it flushes all database operations
    if [ -n "${JELLYFIN_PID:-}" ]; then
        kill -TERM "${JELLYFIN_PID}" 2>/dev/null || true
        for i in {1..10}; do
            kill -0 "${JELLYFIN_PID}" 2>/dev/null || break
            sleep 0.5
        done
    fi

    # 2. Stop Apex Core & Nginx
    [ -n "${APEX_CORE_PID:-}" ] && kill -TERM "${APEX_CORE_PID}" 2>/dev/null || true
    [ -n "${NGINX_PID:-}" ] && kill -QUIT "${NGINX_PID}" 2>/dev/null || true

    # 3. Snapshot local databases to persistent storage
    backup_apex_sqlite

    echo "[Apex] Shutdown complete."
    exit 0
}

trap cleanup SIGTERM SIGINT

# ── 6. Start Jellyfin Media Server on Port 8096 ───────────────────────────────
JELLYFIN_BIN="/jellyfin/jellyfin"
if [ ! -f "${JELLYFIN_BIN}" ] && [ -f "/usr/bin/jellyfin" ]; then
    JELLYFIN_BIN="/usr/bin/jellyfin"
fi

FFMPEG_PATH="/usr/lib/jellyfin-ffmpeg/ffmpeg"
WEBDIR="/jellyfin/jellyfin-web"
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

# ── 7. Start Apex Core Daemon (Go) on Port 8084 ───────────────────────────────
if [ -f "/opt/apex/apex-core" ]; then
    echo "[Apex] Starting Apex Core (Go) on Port 8084..."
    (
        exec /opt/apex/apex-core
    ) &
    APEX_CORE_PID=$!
fi

# ── 8. Start Nginx Public Gateway on Port 7860 ────────────────────────────────
echo "[Apex] Starting Nginx Gateway on Port 7860..."
nginx -g "daemon off;" &
NGINX_PID=$!

echo "[Apex] All platform services dispatched successfully!"
echo "[Apex] Ingress URL: http://0.0.0.0:7860/"

# ── 9. Process Supervisor & Periodic Local Snapshot Loop ──────────────────────
BACKUP_COUNTER=0

while true; do
    # Check Nginx
    if ! kill -0 "${NGINX_PID}" 2>/dev/null; then
        echo "[Apex] CRITICAL: Nginx gateway exited unexpectedly."
        cleanup
    fi

    # Check Jellyfin
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

    # Check Apex Core
    if [ -f "/opt/apex/apex-core" ]; then
        if [ -z "${APEX_CORE_PID:-}" ] || ! kill -0 "${APEX_CORE_PID}" 2>/dev/null; then
            echo "[Apex] Restarting Apex Core Daemon..."
            (
                exec /opt/apex/apex-core
            ) &
            APEX_CORE_PID=$!
        fi
    fi

    # Periodic snapshot for Apex Core local database (every 180 loops * 5s = 15 minutes)
    BACKUP_COUNTER=$((BACKUP_COUNTER + 1))
    if [ "${BACKUP_COUNTER}" -ge 180 ]; then
        BACKUP_COUNTER=0
        backup_apex_sqlite
    fi

    sleep 5
done
