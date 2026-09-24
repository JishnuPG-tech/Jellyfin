#!/bin/bash
set -e

echo "[BOOT] Starting Gateway..."
mkdir -p /data/jellyfin/data /data/jellyfin/config /data/jellyfin/cache /data/jellyfin/log /data/cache 2>/dev/null || true

if [ -f "/health_doctor.py" ]; then
    echo "[HEALTH] Starting Database & Disk Health Doctor daemon..."
    python3 /health_doctor.py &
fi

WEBDIR_OPT=""
if [ -d "/usr/share/jellyfin/web" ]; then
    WEBDIR_OPT="--webdir /usr/share/jellyfin/web"
fi

echo "[HEALTH] Jellyfin starting in background..."
if command -v jellyfin >/dev/null 2>&1; then
    jellyfin --datadir /data/jellyfin/data --configdir /data/jellyfin/config --cachedir /data/jellyfin/cache --logdir /data/jellyfin/log  &
    JELLYFIN_PID=$!
elif [ -f "/usr/bin/jellyfin" ]; then
    /usr/bin/jellyfin --datadir /data/jellyfin/data --configdir /data/jellyfin/config --cachedir /data/jellyfin/cache --logdir /data/jellyfin/log  &
    JELLYFIN_PID=$!
fi

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
    if [ -n "" ] && ! kill -0  2>/dev/null; then
        echo "[CRITICAL] FastAPI Gateway process died! Restarting..."
        python3 -m uvicorn proxy:app --host 127.0.0.1 --port 8000 --workers 2 &
        FASTAPI_PID=$!
    fi

    if [ -n "" ] && ! kill -0  2>/dev/null; then
        echo "[CRITICAL] Nginx process died! Restarting..."
        nginx -g 'daemon off;' -c /nginx.conf &
        NGINX_PID=$!
    fi
    
    if [ -n "" ] && ! kill -0  2>/dev/null; then
        echo "[CRITICAL] TG Streamer process died! Restarting..."
        python3 /tg_streamer.py &
        TG_STREAMER_PID=$!
    fi
    
    if [ -n "" ] && ! kill -0  2>/dev/null; then
        echo "[CRITICAL] Jellyfin process died! Restarting..."
        if command -v jellyfin >/dev/null 2>&1; then
            jellyfin --datadir /data/jellyfin/data --configdir /data/jellyfin/config --cachedir /data/jellyfin/cache --logdir /data/jellyfin/log  &
            JELLYFIN_PID=$!
        fi
    fi

    sleep 10
done
