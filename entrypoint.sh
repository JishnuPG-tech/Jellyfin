#!/bin/bash
set -e

echo "=================================================="
echo " Starting Apex Multi-Project Cloud Suite          "
echo " Hugging Face Space (Persistent Storage Enabled)  "
echo " Included: Stirling-PDF + SnapOtter + Gateway     "
echo "=================================================="

# 1. Initialize persistent storage structure
echo "[Apex] Initializing persistent storage structure..."
mkdir -p /data/Stirling/configs \
         /data/Stirling/logs \
         /data/Stirling/customFiles \
         /data/Stirling/pipeline \
         /data/Stirling/storage \
         /data/Stirling/tessdata \
         /data/files \
         /data/logs \
         /data/ai/models \
         /data/ai/pip-cache \
         /data/ai/venv \
         /data/redis \
         /data/.home \
         /tmp/workspace \
         /tmp/caddy/data \
         /tmp/caddy/config \
         /tmp/stirling-pdf 2>/dev/null || true

# 2. Fix directory ownership and general permissions
echo "[Apex] Configuring volume permissions..."
chown -R snapotter:snapotter /data/files /data/logs /data/ai /data/redis /data/.home /tmp/workspace 2>/dev/null || true
chmod -R 777 /data/Stirling /tmp /data/files /data/logs /data/redis /data/.home 2>/dev/null || true

# 3. Bootstrap SnapOtter Postgres if needed
PGBIN="/usr/lib/postgresql/17/bin"
PGDATA="/data/postgres"

# If pg_control is missing, cluster was not completely initialized
if [ ! -f "$PGDATA/global/pg_control" ]; then
    echo "[Apex] Initializing SnapOtter embedded PostgreSQL 17..."
    rm -rf "$PGDATA"
    install -d -o postgres -g postgres -m 700 "$PGDATA"
    su -s /bin/sh postgres -c "$PGBIN/initdb -D $PGDATA --username=snapotter --encoding=UTF8 --locale=C --auth-local=trust --auth-host=trust"
    {
      echo "listen_addresses = '127.0.0.1'"
      echo "dynamic_shared_memory_type = mmap"
    } >> "$PGDATA/postgresql.conf"
    echo "CREATE DATABASE snapotter OWNER snapotter;" | su -s /bin/sh postgres -c "$PGBIN/postgres --single -D $PGDATA postgres"
    echo "ALTER ROLE snapotter WITH PASSWORD 'snapotter';" | su -s /bin/sh postgres -c "$PGBIN/postgres --single -D $PGDATA postgres"
    echo "[Apex] PostgreSQL 17 initialized."
fi

# Ensure all subdirectories and strict 0700 permissions are guaranteed
mkdir -p "$PGDATA/pg_notify" "$PGDATA/pg_tblspc" "$PGDATA/pg_twophase" "$PGDATA/pg_snapshots" "$PGDATA/pg_commit_ts" "$PGDATA/pg_logical/snapshots" "$PGDATA/pg_logical/mappings" "$PGDATA/pg_wal" "$PGDATA/pg_stat_tmp" "$PGDATA/pg_subtrans" 2>/dev/null || true
chmod 700 "$PGDATA"
chown -R postgres:postgres "$PGDATA"

# Start PostgreSQL 17
echo "[Apex] Starting PostgreSQL 17..."
su -s /bin/sh postgres -c "$PGBIN/postgres -D $PGDATA" &
PG_PID=$!

# Start Redis 8
echo "[Apex] Starting Redis 8..."
su -s /bin/sh snapotter -c "redis-server --dir /data/redis --bind 127.0.0.1 --port 6379 --protected-mode no" &
REDIS_PID=$!

# 4. Bootstrap SnapOtter Python AI venv if needed
AI_VENV="/data/ai/venv"
if [ -d "/opt/venv" ] && [ ! -d "$AI_VENV" ]; then
    echo "[Apex] Bootstrapping SnapOtter AI venv..."
    cp -a /opt/venv "$AI_VENV" 2>/dev/null || true
fi

# Cleanup handler
cleanup() {
    echo "[Apex] Received termination signal. Stopping all services..."
    [ -n "${CADDY_PID:-}" ] && kill -TERM "$CADDY_PID" 2>/dev/null || true
    [ -n "${STIRLING_PID:-}" ] && kill -TERM "$STIRLING_PID" 2>/dev/null || true
    [ -n "${SNAPOTTER_PID:-}" ] && kill -TERM "$SNAPOTTER_PID" 2>/dev/null || true
    [ -n "${REDIS_PID:-}" ] && kill -TERM "$REDIS_PID" 2>/dev/null || true
    [ -n "${PG_PID:-}" ] && kill -TERM "$PG_PID" 2>/dev/null || true
    echo "[Apex] Services stopped."
    exit 0
}

trap cleanup SIGTERM SIGINT

# 5. Start Stirling-PDF on Port 8080
echo "[Apex] Starting Stirling-PDF Backend on Port 8080..."
(
  cd /stirling-app
  exec java -Dstirling.base-path=/data/Stirling/ \
            -Dserver.port=8080 \
            -XX:+ExitOnOutOfMemoryError \
            -XX:+HeapDumpOnOutOfMemoryError \
            -XX:HeapDumpPath=/data/Stirling/configs/heap_dumps \
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

# 6. Start SnapOtter on Port 1349
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

# 7. Start Caddy Gateway on Port 7860
echo "[Apex] Starting Caddy Gateway on Port 7860..."
caddy run --config /app/Caddyfile --adapter caddyfile &
CADDY_PID=$!

echo "[Apex] All services online! Gateway running on Port 7860."

# Monitor processes
while kill -0 "$CADDY_PID" 2>/dev/null && kill -0 "$PG_PID" 2>/dev/null; do
    sleep 3
done

cleanup
