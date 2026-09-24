#!/bin/bash
set -e

echo "=================================================="
echo " 🚀 Starting Apex Cloud Platform (v2.0 Production)"
echo " Stack: Nginx Gateway + Jellyfin + Apex Go Core"
echo "=================================================="

# ── 1. Helper Functions ────────────────────────────────────────────────────────

ensure_jellyfin_dirs_and_libraries() {
    mkdir -p /data/jellyfin/data/root/default/Movies \
             /data/jellyfin/data/root/default/Shows \
             /data/jellyfin/config \
             /data/jellyfin/backups \
             /data/jellyfin/log \
             /data/jellyfin/media/Movies \
             /data/jellyfin/media/Shows \
             /data/jellyfin/.aspnet/DataProtection-Keys \
             /tmp/jellyfin-cache \
             2>/dev/null || true

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
}

locate_jellyfin_db() {
    if [ -f "/data/jellyfin/data/jellyfin.db" ]; then
        echo "/data/jellyfin/data/jellyfin.db"
        return 0
    fi
    if [ -f "/data/jellyfin/jellyfin.db" ]; then
        echo "/data/jellyfin/jellyfin.db"
        return 0
    fi
    if [ -f "/data/jellyfin/data/data/jellyfin.db" ]; then
        echo "/data/jellyfin/data/data/jellyfin.db"
        return 0
    fi
    local found
    found=$(find /data/jellyfin -maxdepth 3 -name "jellyfin.db" 2>/dev/null | head -n 1)
    if [ -n "$found" ] && [ -f "$found" ]; then
        echo "$found"
        return 0
    fi
    return 1
}

# Checks whether jellyfin.db schema conforms to Jellyfin 10.9.11 requirements.
# Returns 0 if compatible or database does not exist.
# Returns 1 if database exists but schema is incompatible (e.g. Users lacks MaxParentalAgeRating).
check_jellyfin_schema() {
    local db_path
    if ! db_path=$(locate_jellyfin_db); then
        echo "[Apex] [Schema Check] No existing jellyfin.db found. Clean database will be initialized on startup."
        return 0
    fi

    echo "[Apex] [Schema Check] Inspecting Jellyfin database at: ${db_path}..."

    if ! command -v sqlite3 >/dev/null 2>&1; then
        echo "[Apex] [Schema Check] Notice: sqlite3 CLI not available in PATH. Skipping schema inspection."
        return 0
    fi

    # Copy to temporary disk location to ensure lock-free inspection across NFS/persistent mounts
    local check_db="/tmp/check_jellyfin.db"
    rm -f "${check_db}" "${check_db}-wal" "${check_db}-shm" 2>/dev/null || true
    cp -f "${db_path}" "${check_db}" 2>/dev/null || true
    [ -f "${db_path}-wal" ] && cp -f "${db_path}-wal" "${check_db}-wal" 2>/dev/null || true

    if [ ! -f "${check_db}" ]; then
        echo "[Apex] [Schema Check] Warning: Unable to create temporary snapshot of database for inspection."
        return 0
    fi

    # Check if Users table exists (case-insensitive)
    local has_users
    has_users=$(sqlite3 "${check_db}" "SELECT count(*) FROM sqlite_master WHERE type='table' AND LOWER(name)='users';" 2>/dev/null || echo "0")
    has_users=$(echo "${has_users}" | tr -d '[:space:]')

    if [ "${has_users}" = "0" ] || [ -z "${has_users}" ]; then
        echo "[Apex] [Schema Check] 'Users' table not found. Database is uninitialized or empty."
        rm -f "${check_db}" "${check_db}-wal" "${check_db}-shm" 2>/dev/null || true
        return 0
    fi

    # Check for MaxParentalAgeRating column strictly required by Jellyfin 10.9.11
    local has_rating_col
    has_rating_col=$(sqlite3 "${check_db}" "SELECT count(*) FROM pragma_table_info('Users') WHERE LOWER(name)='maxparentalagerating';" 2>/dev/null || echo "0")
    has_rating_col=$(echo "${has_rating_col}" | tr -d '[:space:]')

    # Also check for 10.10+/10.11+ column MaxParentalRatingScore for exact diagnosis
    local has_new_col
    has_new_col=$(sqlite3 "${check_db}" "SELECT count(*) FROM pragma_table_info('Users') WHERE LOWER(name)='maxparentalratingscore';" 2>/dev/null || echo "0")
    has_new_col=$(echo "${has_new_col}" | tr -d '[:space:]')

    rm -f "${check_db}" "${check_db}-wal" "${check_db}-shm" 2>/dev/null || true

    if [ "${has_rating_col}" = "0" ]; then
        echo "================================================================================"
        if [ "${has_new_col}" != "0" ]; then
            echo "[Apex] [Schema Check] SCHEMA MISMATCH: 'Users' table contains 'MaxParentalRatingScore' but lacks 'MaxParentalAgeRating'."
            echo "[Apex] [Schema Check] Cause: Database was created or upgraded by Jellyfin 10.10+/10.11+ and cannot be read by 10.9.11."
        else
            echo "[Apex] [Schema Check] SCHEMA MISMATCH: 'Users' table is missing required 'MaxParentalAgeRating' column."
        fi
        echo "================================================================================"
        return 1
    fi

    echo "[Apex] [Schema Check] PASSED: Database has valid Jellyfin 10.9.11 schema (Users.MaxParentalAgeRating verified)."
    return 0
}

# Quarantines incompatible Jellyfin state and performs a clean state reset.
# Preserves virtual media library (/data/jellyfin/media) and Apex state (/data/apex).
quarantine_and_reset_jellyfin_state() {
    local reason="$1"
    local timestamp
    timestamp=$(date +%s)
    local backup_dir="/data/jellyfin/backups/incompatible_schema_${timestamp}"
    mkdir -p "${backup_dir}"

    echo "================================================================================"
    echo "[Apex] CRITICAL: JELLYFIN DATABASE SCHEMA INCOMPATIBILITY DETECTED"
    echo "[Apex] Reason: ${reason}"
    echo "[Apex] Jellyfin 10.9.11 cannot initialize with this incompatible schema."
    echo "[Apex] Creating diagnostic backup at: ${backup_dir}..."
    echo "================================================================================"

    # 1. Back up database and data directory files (preserving media and root)
    if [ -d "/data/jellyfin/data" ]; then
        mkdir -p "${backup_dir}/data"
        find /data/jellyfin/data -maxdepth 1 -type f -exec cp -f {} "${backup_dir}/data/" \; 2>/dev/null || true
    fi

    # 2. Back up configuration directory (contains migrations.xml, system.xml, etc.)
    if [ -d "/data/jellyfin/config" ]; then
        mkdir -p "${backup_dir}/config"
        cp -rf /data/jellyfin/config/* "${backup_dir}/config/" 2>/dev/null || true
    fi

    # 3. Clean Jellyfin database and configuration state
    # NOTE: We MUST remove migrations.xml and system.xml along with the database files
    # so that Jellyfin 10.9.11 performs a true fresh migration and schema initialization.
    echo "[Apex] Resetting Jellyfin database and migration state..."
    rm -rf /data/jellyfin/data/jellyfin.db* 2>/dev/null || true
    rm -rf /data/jellyfin/data/data/jellyfin.db* 2>/dev/null || true
    rm -rf /data/jellyfin/jellyfin.db* 2>/dev/null || true
    rm -rf /data/jellyfin/config/migrations.xml 2>/dev/null || true
    rm -rf /data/jellyfin/config/system.xml 2>/dev/null || true
    rm -rf /data/jellyfin/config/network.xml 2>/dev/null || true
    rm -rf /tmp/jellyfin-cache/* 2>/dev/null || true

    # 4. Re-ensure required directory skeleton and default library definitions
    ensure_jellyfin_dirs_and_libraries

    echo "[Apex] Jellyfin state successfully reset for clean 10.9.11 initialization."
    echo "[Apex] Virtual media library (/data/jellyfin/media) and Apex database (/data/apex) preserved intact."
    echo "================================================================================"
}

start_jellyfin() {
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
    echo "[Apex] Jellyfin dispatched with PID ${JELLYFIN_PID}."
}

# ── 2. Storage Layout Initialization ───────────────────────────────────────────
echo "[Apex] Initializing storage directories..."
ensure_jellyfin_dirs_and_libraries

mkdir -p /data/apex/backups \
         /data/apex/session \
         /data/apex/metadata-cache \
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

# ── 3. Mandatory Secret Key Validation ─────────────────────────────────────────
if [ -z "${APEX_SECRET_KEY:-}" ]; then
    echo "[Apex] FATAL: APEX_SECRET_KEY environment variable is required but missing."
    echo "[Apex] Please configure APEX_SECRET_KEY in Hugging Face Space Secrets or container environment."
    exit 1
fi
echo "[Apex] APEX_SECRET_KEY verified."

# ── 4. SQLite Snapshot Restoration for Apex Core ──────────────────────────────
if [ -f "/data/apex/backups/apex_latest.db" ]; then
    echo "[Apex] Restoring Apex Core SQLite snapshot from /data/apex/backups/apex_latest.db..."
    cp -f /data/apex/backups/apex_latest.db /tmp/apex-db/apex.db
    if [ -f "/data/apex/backups/apex_latest.db-wal" ]; then
        cp -f /data/apex/backups/apex_latest.db-wal /tmp/apex-db/apex.db-wal
    fi
else
    echo "[Apex] Fresh database initialized at /tmp/apex-db/apex.db."
fi

# ── 5. Jellyfin SQLite Schema Compatibility Preflight Check ───────────────────
echo "[Apex] Executing Jellyfin schema compatibility preflight..."
if ! check_jellyfin_schema; then
    quarantine_and_reset_jellyfin_state "Startup preflight detected incompatible Jellyfin database schema"
fi

# ── 6. Graceful Shutdown & Snapshot Backup Handlers ────────────────────────────
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

# ── 7. Start Jellyfin Media Server on Port 8096 ───────────────────────────────
start_jellyfin

# ── 8. Start Apex Core Daemon (Go) on Port 8084 ───────────────────────────────
if [ -f "/opt/apex/apex-core" ]; then
    echo "[Apex] Starting Apex Core (Go) on Port 8084..."
    (
        exec /opt/apex/apex-core
    ) &
    APEX_CORE_PID=$!
fi

# ── 9. Start Nginx Public Gateway on Port 7860 ────────────────────────────────
echo "[Apex] Starting Nginx Gateway on Port 7860..."
nginx -g "daemon off;" &
NGINX_PID=$!

echo "[Apex] All platform services dispatched successfully!"
echo "[Apex] Ingress URL: http://0.0.0.0:7860/"

# ── 10. Process Supervisor & Adaptive Crash Recovery Loop ─────────────────────
BACKUP_COUNTER=0
JELLYFIN_CRASH_COUNT=0
JELLYFIN_UPTIME_TICKS=0
JELLYFIN_RESET_ATTEMPTED=0

while true; do
    # Check Nginx
    if ! kill -0 "${NGINX_PID}" 2>/dev/null; then
        echo "[Apex] CRITICAL: Nginx gateway exited unexpectedly."
        cleanup
    fi

    # Check Jellyfin with schema-aware recovery
    if [ -n "${JELLYFIN_PID:-}" ] && ! kill -0 "${JELLYFIN_PID}" 2>/dev/null; then
        JELLYFIN_CRASH_COUNT=$((JELLYFIN_CRASH_COUNT + 1))
        echo "[Apex] WARNING: Jellyfin process (PID ${JELLYFIN_PID}) exited (consecutive crash count: ${JELLYFIN_CRASH_COUNT})."

        # Allow file handles and WAL to settle
        sleep 1

        # Inspect actual database schema before deciding restart strategy
        if ! check_jellyfin_schema; then
            if [ "${JELLYFIN_RESET_ATTEMPTED}" -eq 0 ]; then
                echo "[Apex] Incompatible database schema confirmed as exit cause."
                quarantine_and_reset_jellyfin_state "Jellyfin exited due to incompatible database schema"
                JELLYFIN_RESET_ATTEMPTED=1
                JELLYFIN_CRASH_COUNT=0
                start_jellyfin
                JELLYFIN_UPTIME_TICKS=0
                sleep 2
                continue
            else
                echo "================================================================================"
                echo "[Apex] FATAL: Jellyfin database schema remains incompatible even after state reset!"
                echo "[Apex] Halting automatic restart loop to prevent resource exhaustion and log thrashing."
                echo "[Apex] Nginx and Apex Core will remain active for portal and diagnostic access."
                echo "================================================================================"
                JELLYFIN_PID=""
                continue
            fi
        fi

        # Non-schema crash: check rapid crash threshold
        if [ "${JELLYFIN_CRASH_COUNT}" -ge 4 ]; then
            echo "[Apex] WARNING: Jellyfin crashed ${JELLYFIN_CRASH_COUNT} times consecutively (non-schema cause)."
            echo "[Apex] Dumping recent Jellyfin log messages:"
            tail -n 25 /data/jellyfin/log/*.log 2>/dev/null || echo "[Apex] No log files available in /data/jellyfin/log/."
            echo "[Apex] Backing off restart for 15 seconds..."
            sleep 15
        fi

        start_jellyfin
        JELLYFIN_UPTIME_TICKS=0
        sleep 2
    elif [ -n "${JELLYFIN_PID:-}" ]; then
        JELLYFIN_UPTIME_TICKS=$((JELLYFIN_UPTIME_TICKS + 1))
        # After 12 ticks (60s) of stable runtime, reset crash counter and reset attempt flag
        if [ "${JELLYFIN_UPTIME_TICKS}" -ge 12 ]; then
            JELLYFIN_CRASH_COUNT=0
            JELLYFIN_RESET_ATTEMPTED=0
        fi
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
