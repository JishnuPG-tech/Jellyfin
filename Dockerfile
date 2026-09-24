# ==============================================================================
# Apex Media Platform - Production Multi-Stage Dockerfile (v2.1)
# Architecture: Caddy Gateway + Jellyfin + Apex Go Core + Stirling-PDF + Enhancer
# ==============================================================================

# ── Stage 1: Build Apex Go Core ────────────────────────────────────────────────
FROM golang:1.22-bookworm AS go-builder

WORKDIR /app
COPY apex_src/ ./
RUN go mod tidy && CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o apex-core ./cmd/apex

# ── Stage 2: Clean Caddy Gateway Binary ────────────────────────────────────────
FROM caddy:2-alpine AS caddy-source

# ── Stage 3: Official Jellyfin Runtime & Media Tools ───────────────────────────
FROM jellyfin/jellyfin:latest AS jellyfin-source

# ── Stage 4: Main Production Image ────────────────────────────────────────────
FROM stirlingtools/stirling-pdf:latest

USER root

# 1. Install Caddy Gateway (Zero apt dependencies)
COPY --from=caddy-source /usr/bin/caddy /usr/local/bin/caddy
RUN chmod +x /usr/local/bin/caddy

# 2. Install Official Jellyfin Media Server & FFmpeg (Zero apt dependencies)
COPY --from=jellyfin-source /jellyfin /opt/jellyfin
COPY --from=jellyfin-source /usr/lib/jellyfin-ffmpeg /usr/lib/jellyfin-ffmpeg
RUN ln -sf /opt/jellyfin/jellyfin /usr/local/bin/jellyfin && \
    ln -sf /usr/lib/jellyfin-ffmpeg/ffmpeg /usr/local/bin/ffmpeg 2>/dev/null || true

# 3. Install Apex Go Core Binary (Zero apt dependencies)
RUN mkdir -p /opt/apex
COPY --from=go-builder /app/apex-core /opt/apex/apex-core
RUN chmod +x /opt/apex/apex-core

# 4. Set up Python environment for PDF Enhancer (FastAPI + React)
RUN pip install --no-cache-dir --break-system-packages \
        httpx fastapi uvicorn python-multipart pydantic pymupdf opencv-python-headless numpy pillow 2>/dev/null || \
    python3 -m pip install --no-cache-dir --break-system-packages \
        httpx fastapi uvicorn python-multipart pydantic pymupdf opencv-python-headless numpy pillow 2>/dev/null || true

# 5. Install Services and Gateway Configuration
RUN mkdir -p /opt/pdf_enhancer /srv/portal /etc/caddy
COPY pdf_enhancer/ /opt/pdf_enhancer/
COPY portal/ /srv/portal/
COPY Caddyfile /etc/caddy/Caddyfile
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# 6. Pre-create required directory layout
RUN mkdir -p /data/Stirling/configs \
             /data/Stirling/logs \
             /data/Stirling/customFiles \
             /data/Stirling/pipeline \
             /data/Stirling/storage \
             /data/Stirling/tessdata \
             /data/jellyfin/data \
             /data/jellyfin/config \
             /data/jellyfin/cache \
             /data/jellyfin/log \
             /data/jellyfin/media/Movies \
             /data/jellyfin/media/Shows \
             /data/apex/backups \
             /data/apex/session \
             /data/apex/metadata-cache \
             /tmp/stirling-pdf \
             /tmp/caddy/data \
             /tmp/caddy/config \
             /tmp/apex-db \
             /tmp/apex-stream-cache

# Environment Configuration
ENV PORT="8080" \
    DATA_DIR="/data" \
    SYSTEM_ROOTURIPATH="/stirling" \
    STIRLING_BASE_PATH="/data/Stirling/" \
    CONFIG_FILE="/data/Stirling/configs/settings.yml" \
    STORAGE_LOCAL_BASEPATH="/data/Stirling/storage" \
    STIRLING_TEMPFILES_DIRECTORY="/tmp/stirling-pdf" \
    ENHANCER_PORT="8082" \
    APEX_CORE_PORT="8084" \
    JELLYFIN_PORT="8096" \
    APEX_MEMORY_CACHE_MB="128" \
    APEX_DISK_CACHE_MB="2048" \
    APEX_PREFETCH_MB="16" \
    APEX_MAX_STREAMS="2" \
    APEX_TELEGRAM_MEDIA_CLIENTS="2" \
    MAX_VIDEO_TRANSCODES="0" \
    MAX_AUDIO_TRANSCODES="1" \
    XDG_DATA_HOME="/tmp/caddy/data" \
    XDG_CONFIG_HOME="/tmp/caddy/config" \
    JAVA_TOOL_OPTIONS="-Dstirling.base-path=/data/Stirling/ -XX:+ExitOnOutOfMemoryError -XX:+HeapDumpOnOutOfMemoryError -XX:HeapDumpPath=/data/Stirling/configs/heap_dumps -XX:+UseG1GC -XX:MaxGCPauseMillis=200 -Dspring.threads.virtual.enabled=true -Djava.awt.headless=true -XX:InitialRAMPercentage=10 -XX:MaxRAMPercentage=40 -XX:MaxMetaspaceSize=384m"

# Hugging Face default ingress port
EXPOSE 7860

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
