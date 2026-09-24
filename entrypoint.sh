#!/bin/bash
set -e

echo "[BOOT] Starting Gateway..."
mkdir -p /data/jellyfin/data /data/jellyfin/config /data/jellyfin/cache /data/jellyfin/log /data/cache 2>/dev/null || true

# ---------------------------------------------------------------------------
# Jellyfin local datadir strategy (HF Spaces persistent /data is network-backed
# and does not honor SQLite file locking; concurrent reads from the web client
# intermittently fail with "SQLite Error 10: disk I/O error").
#
# We run Jellyfin against a LOCAL datadir (reliable locking, fast reads) and
# periodically write a safe online snapshot (sqlite .backup) back to /data so
# the database + config survive container restarts. Media stays on /data.
# ---------------------------------------------------------------------------
LOCAL_ROOT=/opt/jellyfin-local

if [ -f "/health_doctor.py" ]; then
    echo "[HEALTH] Starting Database & Disk Health Doctor daemon..."
    python3 /health_doctor.py &
fi

JELLYFIN_OPTS="--datadir $LOCAL_ROOT/data --configdir $LOCAL_ROOT/config --cachedir $LOCAL_ROOT/cache --logdir $LOCAL_ROOT/log --webdir /usr/share/jellyfin/web"

restore_jellyfin_local() {
    echo "[JELLYFIN] Preparing local datadir at $LOCAL_ROOT ..."
    mkdir -p "$LOCAL_ROOT/data/data" "$LOCAL_ROOT/config" "$LOCAL_ROOT/cache" "$LOCAL_ROOT/log"
    # Restore durable data/config snapshot from the persistent volume if present.
    if [ -f "/data/jellyfin/data/data/jellyfin.db" ]; then
        echo "[JELLYFIN] Restoring database + metadata from persistent volume..."
        cp -a -f /data/jellyfin/data/. "$LOCAL_ROOT/data/"
    fi
    if [ -d "/data/jellyfin/config" ]; then
        cp -a -f /data/jellyfin/config/. "$LOCAL_ROOT/config/" 2>/dev/null || true
    fi
    if [ -d "/data/jellyfin/cache" ]; then
        cp -a -f /data/jellyfin/cache/. "$LOCAL_ROOT/cache/" 2>/dev/null || true
    fi
}

sync_jellyfin_back() {
    # Periodic durable snapshot of the live Jellyfin datadir back to /data.
    # Uses sqlite online backup for the DB; plain copy for config/log/metadata.
    echo "[JELLYFIN] Durable write-back loop started (every 20s)."
    while :; do
        sleep 20
        if [ -f "$LOCAL_ROOT/data/data/jellyfin.db" ]; then
            mkdir -p /data/jellyfin/data/data
            sqlite3 "$LOCAL_ROOT/data/data/jellyfin.db" ".backup /data/jellyfin/data/data/jellyfin.db" 2>/dev/null || \
                cp -f "$LOCAL_ROOT/data/data/jellyfin.db" /data/jellyfin/data/data/jellyfin.db 2>/dev/null || true
        fi
        if [ -d "$LOCAL_ROOT/data" ]; then
            find "$LOCAL_ROOT/data" -mindepth 1 -maxdepth 1 \
                -not -name data \
                -exec cp -a -f {} /data/jellyfin/data/ \; 2>/dev/null || true
        fi
        cp -a -f "$LOCAL_ROOT/config/." /data/jellyfin/config/ 2>/dev/null || true
        cp -a -f "$LOCAL_ROOT/log/." /data/jellyfin/log/ 2>/dev/null || true
    done
}

launch_jellyfin() {
    if command -v jellyfin >/dev/null 2>&1; then
        jellyfin $JELLYFIN_OPTS &
    elif [ -f "/usr/bin/jellyfin" ]; then
        /usr/bin/jellyfin $JELLYFIN_OPTS &
    else
        echo "[CRITICAL] jellyfin binary not found!"
        JELLYFIN_PID=""
        return 1
    fi
    JELLYFIN_PID=$!
}

restore_jellyfin_local

echo "[HEALTH] Jellyfin starting in background (local datadir)..."
launch_jellyfin

sync_jellyfin_back &
SYNC_PID=$!

echo "[HEALTH] Telegram streamer starting in background..."
python3 /tg_streamer.py &
TG_STREAMER_PID=$!

echo "[HEALTH] FastAPI Gateway starting..."
python3 -m uvicorn proxy:app --host 127.0.0.1 --port 8000 --workers 2 &
FASTAPI_PID=$!

echo "[HEALTH] Nginx starting..."
nginx -g 'daemon off;' -c /nginx.conf &
NGINX_PID=$!

echo "[BOOT] All services dispatched. Process Supervisor active."

while true; do
    if [ -n "$FASTAPI_PID" ] && ! kill -0 $FASTAPI_PID 2>/dev/null; then
        echo "[CRITICAL] FastAPI Gateway process died! Restarting..."
        python3 -m uvicorn proxy:app --host 127.0.0.1 --port 8000 --workers 2 &
        FASTAPI_PID=$!
    fi

    if [ -n "$NGINX_PID" ] && ! kill -0 $NGINX_PID 2>/dev/null; then
        echo "[CRITICAL] Nginx process died! Restarting..."
        nginx -g 'daemon off;' -c /nginx.conf &
        NGINX_PID=$!
    fi

    if [ -n "$TG_STREAMER_PID" ] && ! kill -0 $TG_STREAMER_PID 2>/dev/null; then
        echo "[CRITICAL] TG Streamer process died! Restarting..."
        python3 /tg_streamer.py &
        TG_STREAMER_PID=$!
    fi

    if [ -n "$JELLYFIN_PID" ] && ! kill -0 $JELLYFIN_PID 2>/dev/null; then
        echo "[CRITICAL] Jellyfin process died! Restarting..."
        launch_jellyfin
    fi

    if [ -n "$SYNC_PID" ] && ! kill -0 $SYNC_PID 2>/dev/null; then
        echo "[CRITICAL] Jellyfin write-back loop died! Restarting..."
        sync_jellyfin_back &
        SYNC_PID=$!
    fi

    sleep 10
done