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
    if [ -f "/data/apex/backups/apex_latest.db-wal" ]; then
        cp -f /data/apex/backups/apex_latest.db-wal /tmp/apex-db/apex.db-wal
    fi
else
    echo "[Apex] Fresh database initialized at /tmp/apex-db/apex.db."
fi

JELLYFIN_DB="${JELLYFIN_DB_PATH:-/data/jellyfin/data/data/jellyfin.db}"

quarantine_jellyfin_database() {
    local reason="${1:-recovery}"
    local timestamp="$(date +%s)"
    local backup_dir="/data/jellyfin/backups/${reason}_${timestamp}"
    mkdir -p "${backup_dir}"
    [ -f "${JELLYFIN_DB}" ] && mv -f "${JELLYFIN_DB}" "${backup_dir}/" 2>/dev/null || true
    [ -f "${JELLYFIN_DB}-wal" ] && mv -f "${JELLYFIN_DB}-wal" "${backup_dir}/" 2>/dev/null || true
    [ -f "${JELLYFIN_DB}-shm" ] && mv -f "${JELLYFIN_DB}-shm" "${backup_dir}/" 2>/dev/null || true
    echo "[Apex] Jellyfin database state quarantined in ${backup_dir} (${reason})."
}

prepare_jellyfin_database() {
    if ! command -v sqlite3 >/dev/null 2>&1; then
        echo "[Apex] WARNING: sqlite3 is unavailable; skipping Jellyfin database preflight."
        return 0
    fi
    if [ ! -f "${JELLYFIN_DB}" ]; then
        echo "[Apex] [Schema Check] No Jellyfin database at ${JELLYFIN_DB}; Jellyfin will initialize a fresh database."
        return 0
    fi
    echo "[Apex] [Schema Check] Inspecting Jellyfin database at: ${JELLYFIN_DB}..."
    local integrity_output integrity_status=0
    integrity_output="$(sqlite3 "${JELLYFIN_DB}" "PRAGMA integrity_check;" 2>&1)" || integrity_status=$?
    if [ "${integrity_status}" -ne 0 ] || [ "${integrity_output}" != "ok" ]; then
        echo "[Apex] [Schema Check] CRITICAL: Jellyfin SQLite integrity check failed."
        echo "[Apex] [Schema Check] Result: ${integrity_output}"
        quarantine_jellyfin_database "corrupt_db"
        return 1
    fi
    local has_users users_query_status=0
    has_users="$(sqlite3 "${JELLYFIN_DB}" "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='Users';" 2>&1)" || users_query_status=$?
    if [ "${users_query_status}" -ne 0 ]; then
        echo "[Apex] [Schema Check] CRITICAL: SQLite schema query failed: ${has_users}"
        quarantine_jellyfin_database "invalid_schema"
        return 1
    fi
    if [ "${has_users}" = "1" ]; then
        local users_schema schema_query_status=0
        users_schema="$(sqlite3 "${JELLYFIN_DB}" "PRAGMA table_info(Users);" 2>&1)" || schema_query_status=$?
        if [ "${schema_query_status}" -ne 0 ]; then
            echo "[Apex] [Schema Check] CRITICAL: Users schema could not be inspected: ${users_schema}"
            quarantine_jellyfin_database "invalid_users_schema"
            return 1
        fi
        if ! printf '%s\n' "${users_schema}" | grep -qi "MaxParentalAgeRating"; then
            echo "[Apex] [Schema Check] WARNING: Existing Jellyfin database is incompatible with Jellyfin 10.9.11 (missing Users.MaxParentalAgeRating)."
            quarantine_jellyfin_database "incompatible_schema"
            echo "[Apex] [Schema Check] Jellyfin will initialize a fresh compatible database."
            return 1
        fi
        echo "[Apex] [Schema Check] PASSED: Database has valid Jellyfin 10.9.11 schema (Users.MaxParentalAgeRating verified)."
    else
        echo "[Apex] [Schema Check] 'Users' table not found. Database is valid but uninitialized or empty."
    fi
    return 0
}

prepare_jellyfin_database || true
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
        if command -v sqlite3 >/dev/null 2>&1; then
            # Perform true transactional online atomic backup using VACUUM INTO
            rm -f /tmp/apex-db/apex_snapshot.db 2>/dev/null || true
            if sqlite3 /tmp/apex-db/apex.db "VACUUM INTO '/tmp/apex-db/apex_snapshot.db';" 2>/dev/null; then
                mv -f /tmp/apex-db/apex_snapshot.db /data/apex/backups/apex_latest.db
                rm -f /data/apex/backups/apex_latest.db-wal /data/apex/backups/apex_latest.db-shm 2>/dev/null || true
                echo "[Apex] Online atomic SQLite snapshot saved (VACUUM INTO) to /data/apex/backups/apex_latest.db."
                return 0
            fi
            # Fallback to checkpoint + copy if VACUUM INTO was interrupted
            sqlite3 /tmp/apex-db/apex.db "PRAGMA wal_checkpoint(TRUNCATE);" 2>/dev/null || true
        fi
        cp -f /tmp/apex-db/apex.db /tmp/apex-db/apex_backup.db 2>/dev/null || true
        [ -f "/tmp/apex-db/apex.db-wal" ] && cp -f /tmp/apex-db/apex.db-wal /tmp/apex-db/apex_backup.db-wal 2>/dev/null || true
        if [ -f "/tmp/apex-db/apex_backup.db" ]; then
            mv -f /tmp/apex-db/apex_backup.db /data/apex/backups/apex_latest.db
            [ -f "/tmp/apex-db/apex_backup.db-wal" ] && mv -f /tmp/apex-db/apex_backup.db-wal /data/apex/backups/apex_latest.db-wal
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

    # 2. Stop Apex Core (wait up to 5s for clean WAL flush) & Nginx
    if [ -n "${APEX_CORE_PID:-}" ]; then
        kill -TERM "${APEX_CORE_PID}" 2>/dev/null || true
        for i in {1..10}; do
            kill -0 "${APEX_CORE_PID}" 2>/dev/null || break
            sleep 0.5
        done
    fi
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
JELLYFIN_CRASH_COUNT=0
JELLYFIN_UPTIME_TICKS=0

while true; do
    # Check Nginx
    if ! kill -0 "${NGINX_PID}" 2>/dev/null; then
        echo "[Apex] CRITICAL: Nginx gateway exited unexpectedly."
        cleanup
    fi

    # Check Jellyfin with crash recovery
    if [ -n "${JELLYFIN_PID:-}" ] && ! kill -0 "${JELLYFIN_PID}" 2>/dev/null; then
        JELLYFIN_CRASH_COUNT=$((JELLYFIN_CRASH_COUNT + 1))
        echo "[Apex] WARNING: Jellyfin exited (consecutive crash count: ${JELLYFIN_CRASH_COUNT})."
        prepare_jellyfin_database || true
        if [ "${JELLYFIN_CRASH_COUNT}" -ge 4 ]; then
            echo "[Apex] FATAL: Jellyfin crashed ${JELLYFIN_CRASH_COUNT} times consecutively. Forcing clean database recovery."
            quarantine_jellyfin_database "crash_recovery"
            JELLYFIN_CRASH_COUNT=0
        fi
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
        JELLYFIN_UPTIME_TICKS=0
        sleep 2
    else
        JELLYFIN_UPTIME_TICKS=$((JELLYFIN_UPTIME_TICKS + 1))
        if [ "${JELLYFIN_UPTIME_TICKS}" -ge 12 ]; then JELLYFIN_CRASH_COUNT=0; fi
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
