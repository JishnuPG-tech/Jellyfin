# ==============================================================================
# STAGE 1: Extract prebuilt OmniRoute production runtime
# No source compilation. No npm install. No Next.js build. No OOM.
# ==============================================================================
FROM diegosouzapw/omniroute:main AS omniroute-source

# ==============================================================================
# STAGE 2: Final Multi-Service Production Runtime
# Assembles all services using prebuilt artifacts only.
# ==============================================================================
FROM debian:bookworm-slim

ENV XDG_DATA_HOME=/data/share
ENV XDG_CONFIG_HOME=/data/config
ENV XDG_CACHE_HOME=/root/.cache
ENV XDG_STATE_HOME=/data/state
ENV HOME=/root

# 1. Install runtime system packages only — no build toolchain for OmniRoute
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    git \
    python3 \
    python3-pip \
    nginx \
    gnupg \
    sqlite3 \
    redis-server \
 && rm -rf /var/lib/apt/lists/*

# 2. Add Jellyfin official repo & install Jellyfin + FFmpeg
RUN mkdir -p /etc/apt/keyrings \
 && curl -fsSL https://repo.jellyfin.org/jellyfin_team.gpg.key | gpg --dearmor -o /etc/apt/keyrings/jellyfin.gpg \
 && echo "deb [signed-by=/etc/apt/keyrings/jellyfin.gpg arch=amd64] https://repo.jellyfin.org/debian bookworm main" > /etc/apt/sources.list.d/jellyfin.list \
 && apt-get update && apt-get install -y --no-install-recommends jellyfin-server jellyfin-web ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# 3. Install core Python runtime dependencies (Lightweight, no Open WebUI)
RUN pip3 install --no-cache-dir \
    aiohttp pyrogram tgcrypto httpx uvicorn fastapi \
    --break-system-packages

# Install hermes-agent separately with --no-deps to avoid version backtracking
RUN pip3 install --no-cache-dir --no-deps hermes-agent \
    --break-system-packages

# 4. Copy prebuilt OmniRoute production runtime from Stage 1
#    /app inside the upstream image contains the complete standalone production server:
#    .next/, server.js (or package.json start script), node_modules/, and Node binary
COPY --from=omniroute-source /app /omniroute

# 5. Copy Node.js runtime from the OmniRoute image (avoids manual curl download)
COPY --from=omniroute-source /usr/local/bin/node /usr/local/bin/node
COPY --from=omniroute-source /usr/local/bin/npm  /usr/local/bin/npm
COPY --from=omniroute-source /usr/local/bin/npx  /usr/local/bin/npx
COPY --from=omniroute-source /usr/local/lib/node_modules /usr/local/lib/node_modules

RUN mkdir -p /root/.cache /data/cache /data/omniroute
RUN chmod -R 777 /root/.cache /data/cache /omniroute

# 6. Copy Gateway Proxy Application & Entrypoint Scripts
WORKDIR /
COPY entrypoint.sh /entrypoint.sh
COPY nginx.conf /nginx.conf
COPY proxy.py /proxy.py
COPY tg_streamer.py /tg_streamer.py
COPY fix_omniroute.py /fix_omniroute.py
COPY health_doctor.py /health_doctor.py
COPY gateway /gateway
COPY index.html /index.html
RUN chmod +x /entrypoint.sh /fix_omniroute.py /health_doctor.py

EXPOSE 4096

ENTRYPOINT ["/entrypoint.sh"]
