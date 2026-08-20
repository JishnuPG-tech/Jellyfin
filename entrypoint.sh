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
    rm -rf "$PGDATA"
    install -d -o postgres -g postgres -m 700 "$PGDATA"
    su -s /bin/sh postgres -c \
        "$PGBIN/initdb -D $PGDATA --username=snapotter \
         --encoding=UTF8 --locale=C --auth-local=trust --auth-host=trust"
    printf "listen_addresses = '127.0.0.1'\ndynamic_shared_memory_type = mmap\n" \
        >> "$PGDATA/postgresql.conf"
    su -s /bin/sh postgres -c "$PGBIN/postgres --single -D $PGDATA postgres" <<'EOF'
CREATE DATABASE snapotter OWNER snapotter;
ALTER ROLE snapotter WITH PASSWORD 'snapotter';
EOF
    echo "[Apex] PostgreSQL 17 cluster initialized."
}

pg_ensure_dirs() {
    # PostgreSQL will create most dirs itself, but after an unclean shutdown
    # some may be missing. Pre-create all required ones.
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
         /data/files /data/logs /data/redis /data/.home \
         /tmp/workspace /tmp/caddy/data /tmp/caddy/config /tmp/stirling-pdf \
         2>/dev/null || true

echo "[Apex] Configuring volume permissions..."
chown -R snapotter:snapotter /data/files /data/logs /data/redis /data/.home \
    /tmp/workspace 2>/dev/null || true

# ── 2. PostgreSQL cluster setup ────────────────────────────────────────────────
# CRITICAL: Check FIRST, then ensure subdirs, then start.
# Previous bug: health check ran before mkdir → always re-init.
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
su -s /bin/sh postgres -c "$PGBIN/postgres -D $PGDATA" &
PG_PID=$!

# Wait up to 300s for PostgreSQL to accept connections.
# During crash recovery PG does a long fsync (can take 60-120s) — all
# incoming pg_isready attempts get "FATAL: database system is starting up".
# This is NORMAL — do NOT reinit. Only reinit if the PG process itself dies.
echo "[Apex] Waiting for PostgreSQL to accept connections (up to 300s)..."
PG_READY=0
for i in $(seq 1 300); do
    if su -s /bin/sh postgres -c "$PGBIN/pg_isready -h 127.0.0.1 -p 5432 -q" 2>/dev/null; then
        echo "[Apex] PostgreSQL ready after ${i}s."
        PG_READY=1
        break
    fi
    # Only reinit if the PG postmaster process itself has exited (genuine crash)
    if ! kill -0 "$PG_PID" 2>/dev/null; then
        echo "[Apex] PostgreSQL process died — reinitializing cluster..."
        pg_init
        pg_ensure_dirs
        su -s /bin/sh postgres -c "$PGBIN/postgres -D $PGDATA" &
        PG_PID=$!
    fi
    [ $((i % 10)) -eq 0 ] && echo "[Apex] Still waiting for PostgreSQL... (${i}s elapsed, recovery in progress)"
    sleep 1
done

if [ "$PG_READY" -eq 0 ]; then
    echo "[Apex] FATAL: PostgreSQL failed to start within 300s. Aborting."
    cleanup
fi

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
export AUTH_ENABLED="true"
export DEFAULT_USERNAME="admin"
export DEFAULT_PASSWORD="admin"

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

echo "[Apex] All services online! Gateway → http://0.0.0.0:7860"
echo "[Apex]   Portal     → /"
echo "[Apex]   SnapOtter  → / (AI tools)"
echo "[Apex]   Stirling   → /stirling"

# ── 8. Process monitor ─────────────────────────────────────────────────────────
while true; do
    if ! kill -0 "$CADDY_PID" 2>/dev/null; then
        echo "[Apex] Caddy exited — shutting down."; cleanup
    fi
    if ! kill -0 "$PG_PID" 2>/dev/null; then
        echo "[Apex] PostgreSQL exited — shutting down."; cleanup
    fi
    sleep 5
done
