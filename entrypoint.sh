#!/bin/bash
set -e

echo "=================================================="
echo " Starting Apex Multi-Project Cloud Suite          "
echo " Hugging Face Space (Persistent Storage Enabled)  "
echo " Included: Stirling-PDF + SnapOtter + Gateway     "
echo "=================================================="

PGBIN="/usr/lib/postgresql/17/bin"
PGDATA="/data/postgres"

# ── Helpers ────────────────────────────────────────────────────────────────────

pg_init() {
    echo "[Apex] Initializing fresh PostgreSQL 17 cluster..."
    # Safe wipe: delete contents recursively but tolerate protected files on
    # persistent volumes (rm -rf can fail silently on some HF volume mounts).
    find "$PGDATA" -mindepth 1 -delete 2>/dev/null || rm -rf "${PGDATA:?}"/* "${PGDATA:?}"/.[!.]* 2>/dev/null || true
    rmdir "$PGDATA" 2>/dev/null || true
    install -d -o postgres -g postgres -m 700 "$PGDATA"
    su -s /bin/sh postgres -c \
        "$PGBIN/initdb -D $PGDATA --username=snapotter \
         --encoding=UTF8 --locale=C --auth-local=trust --auth-host=trust"

    # Container-optimised postgresql.conf settings
    cat >> "$PGDATA/postgresql.conf" <<'PGCONF'
listen_addresses        = '127.0.0.1'
dynamic_shared_memory_type = mmap
unix_socket_directories = '/tmp'
data_sync_retry         = on
full_page_writes        = off
synchronous_commit      = off
wal_level               = minimal
max_wal_senders         = 0
shared_buffers          = 128MB
effective_cache_size    = 512MB
checkpoint_timeout      = 15min
checkpoint_completion_target = 0.9
PGCONF

    # Bootstrap: start a temporary server, create DB/user via psql, stop it.
    # Much more robust than postgres --single which requires an existing DB.
    echo "[Apex] Bootstrapping snapotter database..."
    su -s /bin/sh postgres -c \
        "$PGBIN/pg_ctl -D $PGDATA -w -t 60 start -l /tmp/pg_bootstrap.log" || {
        echo "[Apex] Bootstrap server failed to start. Log:"; cat /tmp/pg_bootstrap.log || true; exit 1
    }
    su -s /bin/sh postgres -c \
        "$PGBIN/psql -h 127.0.0.1 -p 5432 -d postgres -c \"CREATE DATABASE snapotter OWNER snapotter;\""
    su -s /bin/sh postgres -c \
        "$PGBIN/psql -h 127.0.0.1 -p 5432 -d postgres -c \"ALTER ROLE snapotter WITH PASSWORD 'snapotter';\""
    su -s /bin/sh postgres -c \
        "$PGBIN/pg_ctl -D $PGDATA -w -t 30 stop"
    echo "[Apex] PostgreSQL 17 cluster initialized."
}

# Idempotent DB/role check — runs after every PG startup.
# Catches cases where a prior initdb succeeded but bootstrap was interrupted.
pg_ensure_db() {
    echo "[Apex] Verifying snapotter database and role..."
    # Create role if missing (initdb --username sets it, but guard anyway)
    su -s /bin/sh postgres -c \
        "$PGBIN/psql -h 127.0.0.1 -p 5432 -d postgres -tAc \
        \"SELECT 1 FROM pg_roles WHERE rolname='snapotter'\"" 2>/dev/null | grep -q 1 || \
    su -s /bin/sh postgres -c \
        "$PGBIN/psql -h 127.0.0.1 -p 5432 -d postgres -c \
        \"CREATE ROLE snapotter WITH LOGIN PASSWORD 'snapotter';\"" 2>/dev/null || true

    # Create database if missing
    if ! su -s /bin/sh postgres -c \
        "$PGBIN/psql -h 127.0.0.1 -p 5432 -d postgres -tAc \
        \"SELECT 1 FROM pg_database WHERE datname='snapotter'\"" 2>/dev/null | grep -q 1; then
        echo "[Apex] snapotter database missing — creating now..."
        su -s /bin/sh postgres -c \
            "$PGBIN/psql -h 127.0.0.1 -p 5432 -d postgres -c \
            \"CREATE DATABASE snapotter OWNER snapotter;\""
        echo "[Apex] snapotter database created."
    else
        echo "[Apex] snapotter database OK."
    fi
}


pg_ensure_dirs() {
    # Pre-create WAL/transaction subdirectories that may vanish after
    # an unclean shutdown on a persistent volume.
    mkdir -p \
        "$PGDATA/pg_commit_ts" "$PGDATA/pg_dynshmem" \
        "$PGDATA/pg_logical/snapshots" "$PGDATA/pg_logical/mappings" \
        "$PGDATA/pg_multixact/members" "$PGDATA/pg_multixact/offsets" \
        "$PGDATA/pg_notify" "$PGDATA/pg_replslot" "$PGDATA/pg_serial" \
        "$PGDATA/pg_snapshots" "$PGDATA/pg_stat" "$PGDATA/pg_stat_tmp" \
        "$PGDATA/pg_subtrans" "$PGDATA/pg_tblspc" "$PGDATA/pg_twophase" \
        "$PGDATA/pg_wal/archive_status" "$PGDATA/pg_wal/summaries" \
        "$PGDATA/pg_xact" 2>/dev/null || true
    chmod 700 "$PGDATA"
    chown -R postgres:postgres "$PGDATA"
}

pg_start() {
    # Use -k /tmp so the Unix socket is on tmpfs (avoids HF persistent-volume
    # locking quirks that can cause "postmaster.pid" FATAL errors).
    su -s /bin/sh postgres -c \
        "HOME=/var/lib/postgresql $PGBIN/postgres -D $PGDATA -k /tmp" &
    PG_PID=$!
}

pg_wait_ready() {
    # Wait up to 300s. During crash recovery (fsync phase) pg_isready returns
    # "the database system is starting up" — this is NORMAL. Only reinitialise
    # if pg_control is gone (genuine data loss), otherwise just restart.
    echo "[Apex] Waiting for PostgreSQL to accept connections (up to 300s)..."
    PG_READY=0
    for i in $(seq 1 300); do
        if su -s /bin/sh postgres -c \
               "$PGBIN/pg_isready -h 127.0.0.1 -p 5432 -U snapotter -q" \
               2>/dev/null; then
            echo "[Apex] PostgreSQL ready after ${i}s."
            PG_READY=1
            break
        fi

        # If the postmaster died mid-recovery, decide whether to restart or reinit
        if ! kill -0 "$PG_PID" 2>/dev/null; then
            if [ -f "$PGDATA/global/pg_control" ]; then
                # Data is intact — just restart; do NOT call pg_init (no data wipe)
                echo "[Apex] PostgreSQL crashed mid-recovery — restarting (data intact)..."
                pg_ensure_dirs
                pg_start
            else
                # pg_control is gone — data is unrecoverable, reinitialise
                echo "[Apex] PostgreSQL data lost — reinitialising cluster..."
                pg_init
                pg_ensure_dirs
                pg_start
            fi
        fi

        [ $((i % 10)) -eq 0 ] && \
            echo "[Apex] Still waiting for PostgreSQL... (${i}s elapsed, recovery in progress)"
        sleep 1
    done
}

cleanup() {
    echo "[Apex] Received termination signal. Stopping all services..."
    for pid_var in CADDY_PID STIRLING_PID SNAPOTTER_PID REDIS_PID PG_PID; do
        pid="${!pid_var:-}"
        [ -n "$pid" ] && kill -TERM "$pid" 2>/dev/null || true
    done
    echo "[Apex] Services stopped."
    exit 0
}

# ── 1. Persistent storage layout ───────────────────────────────────────────────
echo "[Apex] Initializing persistent storage structure..."
mkdir -p /data/Stirling/configs /data/Stirling/logs /data/Stirling/customFiles \
         /data/Stirling/pipeline /data/Stirling/storage \
         /data/files /data/logs /data/redis /data/.home /data/ai \
         /tmp/workspace /tmp/caddy/data /tmp/caddy/config /tmp/stirling-pdf \
         /tmp/pg-socket \
         2>/dev/null || true

echo "[Apex] Configuring volume permissions..."
chown -R snapotter:snapotter /data/files /data/logs /data/redis /data/.home \
    /data/ai /tmp/workspace 2>/dev/null || true
chmod 755 /data/ai 2>/dev/null || true
# Ensure postgres user owns its socket dir
chown postgres:postgres /tmp/pg-socket 2>/dev/null || true
chmod 755 /tmp/pg-socket 2>/dev/null || true

# ── 2. PostgreSQL cluster setup ────────────────────────────────────────────────
# Check FIRST, then ensure subdirs, then start.
if [ -f "$PGDATA/global/pg_control" ]; then
    echo "[Apex] Existing PostgreSQL cluster found — ensuring directory integrity..."
    pg_ensure_dirs
else
    echo "[Apex] PostgreSQL cluster not found — initializing..."
    pg_init
    pg_ensure_dirs
fi

trap cleanup SIGTERM SIGINT

# ── 3. Start PostgreSQL 17 ─────────────────────────────────────────────────────
echo "[Apex] Starting PostgreSQL 17..."
pg_start
pg_wait_ready

if [ "$PG_READY" -eq 0 ]; then
    echo "[Apex] FATAL: PostgreSQL failed to start within 300s. Aborting."
    cleanup
fi

# Always verify snapotter DB/role exist — handles partial init from prior boots
pg_ensure_db

# ── 4. Start Redis 8 ───────────────────────────────────────────────────────────
echo "[Apex] Starting Redis 8..."
su -s /bin/sh snapotter -c \
    "redis-server --dir /data/redis --bind 127.0.0.1 --port 6379 --protected-mode no" &
REDIS_PID=$!

# ── 5. Start Stirling-PDF on port 8080 ────────────────────────────────────────
echo "[Apex] Starting Stirling-PDF Backend on Port 8080..."
(
  cd /stirling-app
  exec java \
    -Dstirling.base-path=/data/Stirling/ \
    -Dserver.port=8080 \
    -XX:+UseG1GC \
    -XX:MaxGCPauseMillis=200 \
    -Dspring.threads.virtual.enabled=true \
    -Djava.awt.headless=true \
    -XX:InitialRAMPercentage=5 \
    -XX:MaxRAMPercentage=25 \
    -XX:MaxMetaspaceSize=256m \
    -cp "/stirling-app/app.jar:/stirling-app/lib/*" \
    stirling.software.SPDF.SPDFApplication
) &
STIRLING_PID=$!

# ── 6. Start SnapOtter on port 1349 ───────────────────────────────────────────
echo "[Apex] Starting SnapOtter Engine on Port 1349..."
export DATABASE_URL="postgres://snapotter:snapotter@127.0.0.1:5432/snapotter"
export REDIS_URL="redis://127.0.0.1:6379"
export DATA_DIR="/data"
export WORKSPACE_PATH="/tmp/workspace"
export HOME="/data/.home"
export PORT="1349"
export AUTH_ENABLED="${AUTH_ENABLED:-true}"
export DEFAULT_USERNAME="${SNAPOTTER_USERNAME:-admin}"
export DEFAULT_PASSWORD="${SNAPOTTER_PASSWORD:-admin}"

(
  cd /app/apps/api
  exec su -s /bin/sh snapotter -c \
    "export HOME=/data/.home && \
     ./node_modules/.bin/tsx \
       --import ./src/tracing.ts \
       --import ./src/instrument.ts \
       src/index.ts"
) &
SNAPOTTER_PID=$!

# ── 7. Start Caddy Gateway on port 7860 ───────────────────────────────────────
echo "[Apex] Starting Caddy Gateway on Port 7860..."
caddy run --config /etc/caddy/Caddyfile --adapter caddyfile &
CADDY_PID=$!

echo "[Apex] All services dispatched!"
echo "[Apex]   Portal     → https://jishnupg-apex.hf.space/"
echo "[Apex]   SnapOtter  → https://jishnupg-apex.hf.space/ (AI tools)"
echo "[Apex]   Stirling   → https://jishnupg-apex.hf.space/stirling"

# ── 8. Process supervisor ──────────────────────────────────────────────────────
# Monitor all critical services and restart where possible.
while true; do
    # Caddy: gateway must stay up — if it dies, shut everything down
    if ! kill -0 "$CADDY_PID" 2>/dev/null; then
        echo "[Apex] CRITICAL: Caddy gateway exited — shutting down."
        cleanup
    fi

    # PostgreSQL: restart without data wipe if pg_control is intact
    if ! kill -0 "$PG_PID" 2>/dev/null; then
        if [ -f "$PGDATA/global/pg_control" ]; then
            echo "[Apex] WARNING: PostgreSQL exited — restarting (data intact)..."
            pg_ensure_dirs
            pg_start
        else
            echo "[Apex] CRITICAL: PostgreSQL data lost — reinitialising and restarting..."
            pg_init
            pg_ensure_dirs
            pg_start
        fi
    fi

    # SnapOtter: auto-restart on crash
    if ! kill -0 "$SNAPOTTER_PID" 2>/dev/null; then
        echo "[Apex] WARNING: SnapOtter exited — restarting..."
        (
          cd /app/apps/api
          exec su -s /bin/sh snapotter -c \
            "export HOME=/data/.home && \
             ./node_modules/.bin/tsx \
               --import ./src/tracing.ts \
               --import ./src/instrument.ts \
               src/index.ts"
        ) &
        SNAPOTTER_PID=$!
    fi

    # Stirling-PDF: auto-restart on crash
    if ! kill -0 "$STIRLING_PID" 2>/dev/null; then
        echo "[Apex] WARNING: Stirling-PDF exited — restarting..."
        (
          cd /stirling-app
          exec java \
            -Dstirling.base-path=/data/Stirling/ \
            -Dserver.port=8080 \
            -XX:+UseG1GC -XX:MaxGCPauseMillis=200 \
            -Dspring.threads.virtual.enabled=true \
            -Djava.awt.headless=true \
            -XX:InitialRAMPercentage=5 -XX:MaxRAMPercentage=25 \
            -XX:MaxMetaspaceSize=256m \
            -cp "/stirling-app/app.jar:/stirling-app/lib/*" \
            stirling.software.SPDF.SPDFApplication
        ) &
        STIRLING_PID=$!
    fi

    sleep 5
done
