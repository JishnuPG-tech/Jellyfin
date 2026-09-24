#!/bin/bash
set -e

echo "[BOOT] Starting Gateway..."
mkdir -p /data/jellyfin/data /data/jellyfin/config /data/jellyfin/cache /data/jellyfin/log /data/cache 2>/dev/null || true

# Step 1: Health Doctor
if [ -f "/health_doctor.py" ]; then
    echo "[HEALTH] Starting Database & Disk Health Doctor daemon..."
    python3 /health_doctor.py > /data/cache/health_doctor.log 2>&1 &
fi

# Step 2: Jellyfin
WEBDIR_OPT=""
if [ -d "/usr/share/jellyfin/web" ]; then
    WEBDIR_OPT="--webdir /usr/share/jellyfin/web"
fi

echo "[HEALTH] Jellyfin starting in background..."
if command -v jellyfin >/dev/null 2>&1; then
    jellyfin --datadir /data/jellyfin/data --configdir /data/jellyfin/config --cachedir /data/jellyfin/cache --logdir /data/jellyfin/log  > /data/jellyfin/log/jellyfin.log 2>&1 &
    JELLYFIN_PID=$!
elif [ -f "/usr/bin/jellyfin" ]; then
    /usr/bin/jellyfin --datadir /data/jellyfin/data --configdir /data/jellyfin/config --cachedir /data/jellyfin/cache --logdir /data/jellyfin/log  > /data/jellyfin/log/jellyfin.log 2>&1 &
    JELLYFIN_PID=$!
fi

# Step 3: TG Streamer
echo "[HEALTH] Telegram streamer starting in background..."
python3 /tg_streamer.py > /data/cache/tg_streamer.log 2>&1 &
TG_STREAMER_PID=$!

# Step 4: FastAPI Gateway
echo "[HEALTH] FastAPI Gateway starting..."
python3 -m uvicorn proxy:app --host 127.0.0.1 --port 8000 --workers 2 > /data/cache/fastapi_gateway.log 2>&1 &
FASTAPI_PID=$!

# Step 5: NGINX
echo "[HEALTH] Nginx starting..."
nginx -g 'daemon off;' -c /nginx.conf &
NGINX_PID=$!

echo "[BOOT] All services dispatched. Process Supervisor active."

while true; do
    if [ -n "" ] && ! kill -0  2>/dev/null; then
        echo "[CRITICAL] FastAPI Gateway process died! Restarting..."
        python3 -m uvicorn proxy:app --host 127.0.0.1 --port 8000 --workers 2 > /data/cache/fastapi_gateway.log 2>&1 &
        FASTAPI_PID=$!
    fi

    if [ -n "" ] && ! kill -0  2>/dev/null; then
        echo "[CRITICAL] Nginx process died! Restarting..."
        nginx -g 'daemon off;' -c /nginx.conf &
        NGINX_PID=$!
    fi
    
    if [ -n "" ] && ! kill -0  2>/dev/null; then
        echo "[CRITICAL] TG Streamer process died! Restarting..."
        python3 /tg_streamer.py > /data/cache/tg_streamer.log 2>&1 &
        TG_STREAMER_PID=$!
    fi
    
    if [ -n "" ] && ! kill -0  2>/dev/null; then
        echo "[CRITICAL] Jellyfin process died! Restarting..."
        if command -v jellyfin >/dev/null 2>&1; then
            jellyfin --datadir /data/jellyfin/data --configdir /data/jellyfin/config --cachedir /data/jellyfin/cache --logdir /data/jellyfin/log  > /data/jellyfin/log/jellyfin.log 2>&1 &
            JELLYFIN_PID=$!
        fi
    fi

    sleep 10
done
