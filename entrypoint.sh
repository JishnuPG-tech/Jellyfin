#!/bin/bash
set -e

echo "=================================================="
echo " Starting Apex Multi-Project Cloud Suite          "
echo " Hugging Face Space (Persistent Storage Enabled)  "
echo " Included: Stirling-PDF + SnapOtter + Gateway     "
echo "=================================================="

PGBIN="/usr/lib/postgresql/17/bin"
PGDATA="/data/postgres"

# Helper: full PostgreSQL cluster health check
pg_cluster_is_healthy() {
    # Verify all required subdirectories exist (not just pg_control)
    local required_dirs=(
        "base" "global" "pg_commit_ts" "pg_dynshmem"
        "pg_logical" "pg_logical/snapshots" "pg_logical/mappings"
        "pg_multixact" "pg_multixact/members" "pg_multixact/offsets"
        "pg_notify" "pg_replslot" "pg_serial" "pg_snapshots"
        "pg_stat" "pg_stat_tmp" "pg_subtrans" "pg_tblspc"
        "pg_twophase" "pg_wal" "pg_wal/archive_status" "pg_xact"
    )
    [ -f "$PGDATA/global/pg_control" ] || return 1
    for dir in "${required_dirs[@]}"; do
        [ -d "$PGDATA/$dir" ] || return 1
    done
    return 0
}

# 1. Initialize persistent storage structure
echo "[Apex] Initializing persistent storage structure..."
mkdir -p /data/Stirling/configs \
         /data/Stirling/logs \
         /data/Stirling/customFiles \
         /data/Stirling/pipeline \
         /data/Stirling/storage \
         /data/files \
         /data/logs \
         /data/ai/models \
         /data/redis \
         /data/.home \
         /tmp/workspace \
         /tmp/caddy/data \
         /tmp/caddy/config \
         /tmp/stirling-pdf 2>/dev/null || true

echo "[Apex] Configuring volume permissions..."
chown -R snapotter:snapotter /data/files /data/logs /data/ai /data/redis /data/.home /tmp/workspace 2>/dev/null || true
chmod -R 777 /data/Stirling /tmp /data/files /data/logs /data/redis /data/.home 2>/dev/null || true

# 2. Bootstrap or repair PostgreSQL cluster
if pg_cluster_is_healthy; then
    echo "[Apex] Existing PostgreSQL cluster looks healthy."
else
    echo "[Apex] PostgreSQL cluster missing or corrupted — reinitializing from scratch..."
    rm -rf "$PGDATA"
    install -d -o postgres -g postgres -m 700 "$PGDATA"
    su -s /bin/sh postgres -c "$PGBIN/initdb -D $PGDATA --username=snapotter --encoding=UTF8 --locale=C --auth-local=trust --auth-host=trust"
    {
      echo "listen_addresses = '127.0.0.1'"
      echo "dynamic_shared_memory_type = mmap"
    } >> "$PGDATA/postgresql.conf"
    su -s /bin/sh postgres -c "$PGBIN/postgres --single -D $PGDATA postgres" <<EOF
CREATE DATABASE snapotter OWNER snapotter;
ALTER ROLE snapotter WITH PASSWORD 'snapotter';
EOF
    echo "[Apex] PostgreSQL 17 cluster initialized."
fi

# Ensure ALL required subdirs exist and ownership is correct before start
mkdir -p \
    "$PGDATA/pg_commit_ts" "$PGDATA/pg_dynshmem" \
    "$PGDATA/pg_logical/snapshots" "$PGDATA/pg_logical/mappings" \
    "$PGDATA/pg_multixact/members" "$PGDATA/pg_multixact/offsets" \
    "$PGDATA/pg_notify" "$PGDATA/pg_replslot" "$PGDATA/pg_serial" \
    "$PGDATA/pg_snapshots" "$PGDATA/pg_stat" "$PGDATA/pg_stat_tmp" \
    "$PGDATA/pg_subtrans" "$PGDATA/pg_tblspc" "$PGDATA/pg_twophase" \
    "$PGDATA/pg_wal/archive_status" "$PGDATA/pg_wal/summaries" "$PGDATA/pg_xact" \
    2>/dev/null || true
chmod 700 "$PGDATA"
chown -R postgres:postgres "$PGDATA"

# Cleanup handler
cleanup() {
    echo "[Apex] Received termination signal. Stopping all services..."
    [ -n "${CADDY_PID:-}" ]    && kill -TERM "$CADDY_PID" 2>/dev/null || true
    [ -n "${STIRLING_PID:-}" ] && kill -TERM "$STIRLING_PID" 2>/dev/null || true
    [ -n "${SNAPOTTER_PID:-}" ]&& kill -TERM "$SNAPOTTER_PID" 2>/dev/null || true
    [ -n "${REDIS_PID:-}" ]    && kill -TERM "$REDIS_PID" 2>/dev/null || true
    [ -n "${PG_PID:-}" ]       && kill -TERM "$PG_PID" 2>/dev/null || true
    echo "[Apex] Services stopped."
    exit 0
}
trap cleanup SIGTERM SIGINT

# 3. Start PostgreSQL 17
echo "[Apex] Starting PostgreSQL 17..."
su -s /bin/sh postgres -c "$PGBIN/postgres -D $PGDATA" &
PG_PID=$!

# Wait for PostgreSQL to be ready (up to 60s)
echo "[Apex] Waiting for PostgreSQL to accept connections..."
for i in $(seq 1 60); do
    if su -s /bin/sh postgres -c "$PGBIN/pg_isready -h 127.0.0.1 -p 5432" >/dev/null 2>&1; then
        echo "[Apex] PostgreSQL ready after ${i}s."
        break
    fi
    # If postgres died already, reinitialize and restart
    if ! kill -0 "$PG_PID" 2>/dev/null; then
        echo "[Apex] PostgreSQL crashed during startup — reinitializing cluster..."
        rm -rf "$PGDATA"
        install -d -o postgres -g postgres -m 700 "$PGDATA"
        su -s /bin/sh postgres -c "$PGBIN/initdb -D $PGDATA --username=snapotter --encoding=UTF8 --locale=C --auth-local=trust --auth-host=trust"
        {
          echo "listen_addresses = '127.0.0.1'"
          echo "dynamic_shared_memory_type = mmap"
        } >> "$PGDATA/postgresql.conf"
        su -s /bin/sh postgres -c "$PGBIN/postgres --single -D $PGDATA postgres" <<EOF
CREATE DATABASE snapotter OWNER snapotter;
ALTER ROLE snapotter WITH PASSWORD 'snapotter';
EOF
        mkdir -p \
            "$PGDATA/pg_commit_ts" "$PGDATA/pg_dynshmem" \
            "$PGDATA/pg_logical/snapshots" "$PGDATA/pg_logical/mappings" \
            "$PGDATA/pg_multixact/members" "$PGDATA/pg_multixact/offsets" \
            "$PGDATA/pg_notify" "$PGDATA/pg_replslot" "$PGDATA/pg_serial" \
            "$PGDATA/pg_snapshots" "$PGDATA/pg_stat" "$PGDATA/pg_stat_tmp" \
            "$PGDATA/pg_subtrans" "$PGDATA/pg_tblspc" "$PGDATA/pg_twophase" \
            "$PGDATA/pg_wal/archive_status" "$PGDATA/pg_wal/summaries" "$PGDATA/pg_xact" \
            2>/dev/null || true
        chmod 700 "$PGDATA"
        chown -R postgres:postgres "$PGDATA"
        su -s /bin/sh postgres -c "$PGBIN/postgres -D $PGDATA" &
        PG_PID=$!
    fi
    sleep 1
done

# 4. Start Redis 8
echo "[Apex] Starting Redis 8..."
su -s /bin/sh snapotter -c "redis-server --dir /data/redis --bind 127.0.0.1 --port 6379 --protected-mode no" &
REDIS_PID=$!

# 5. Bootstrap SnapOtter Python AI venv if needed
if [ -d "/opt/venv" ] && [ ! -d "/data/ai/venv" ]; then
    echo "[Apex] Bootstrapping SnapOtter AI venv..."
    cp -a /opt/venv /data/ai/venv 2>/dev/null || true
fi

# 6. Start Stirling-PDF on Port 8080
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

# 7. Start SnapOtter on Port 1349
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
  exec su -s /bin/sh snapotter -c "export HOME=/data/.home && ./node_modules/.bin/tsx --import ./src/tracing.ts --import ./src/instrument.ts src/index.ts"
) &
SNAPOTTER_PID=$!

# 8. Start Caddy Gateway on Port 7860
echo "[Apex] Starting Caddy Gateway on Port 7860..."
caddy run --config /app/Caddyfile --adapter caddyfile &
CADDY_PID=$!

echo "[Apex] All services online! Gateway running on Port 7860."

# Monitor: restart if postgres or caddy die
while true; do
    if ! kill -0 "$CADDY_PID" 2>/dev/null; then
        echo "[Apex] Caddy died — shutting down."
        cleanup
    fi
    if ! kill -0 "$PG_PID" 2>/dev/null; then
        echo "[Apex] PostgreSQL died — shutting down."
        cleanup
    fi
    sleep 5
done
