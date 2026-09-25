FROM debian:bookworm-slim

ENV XDG_DATA_HOME=/data/share
ENV XDG_CONFIG_HOME=/data/config
ENV XDG_CACHE_HOME=/root/.cache
ENV XDG_STATE_HOME=/data/state
ENV HOME=/root

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    git \
    python3 \
    python3-pip \
    nginx \
    gnupg \
    sqlite3 \
 && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /etc/apt/keyrings \
 && curl -fsSL https://repo.jellyfin.org/jellyfin_team.gpg.key | gpg --dearmor -o /etc/apt/keyrings/jellyfin.gpg \
 && echo "deb [signed-by=/etc/apt/keyrings/jellyfin.gpg arch=amd64] https://repo.jellyfin.org/debian bookworm main" > /etc/apt/sources.list.d/jellyfin.list \
 && apt-get update && apt-get install -y --no-install-recommends jellyfin-server jellyfin-web ffmpeg \
 && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir \
    aiohttp pyrogram tgcrypto httpx uvicorn[standard] fastapi websockets \
    --break-system-packages

RUN mkdir -p /root/.cache /data/cache
RUN chmod -R 777 /root/.cache /data/cache

WORKDIR /
COPY entrypoint.sh /entrypoint.sh
COPY nginx.conf /nginx.conf
COPY proxy.py /proxy.py
COPY tg_streamer.py /tg_streamer.py
COPY health_doctor.py /health_doctor.py
COPY apex_stream /apex_stream
COPY gateway /gateway
RUN chmod +x /entrypoint.sh /health_doctor.py

EXPOSE 7860

ENTRYPOINT ["/entrypoint.sh"]
