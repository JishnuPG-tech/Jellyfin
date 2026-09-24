# ==============================================================================
# Apex Media Platform - Production Dockerfile (v2.1)
# Architecture: Nginx Gateway + Jellyfin + Apex Go Core + Stirling-PDF + Enhancer
# ==============================================================================

# ── Stage 1: Build Apex Go Core ────────────────────────────────────────────────
FROM golang:1.22-bookworm AS go-builder

WORKDIR /app
COPY apex_src/go.mod apex_src/go.sum* ./
RUN go mod download 2>/dev/null || true
COPY apex_src/ ./
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o apex-core ./cmd/apex

# ── Stage 2: Main Production Image ────────────────────────────────────────────
FROM stirlingtools/stirling-pdf:latest

USER root

# Install system dependencies: Nginx, SQLite3, Python, FFMpeg tools
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        nginx \
        sqlite3 \
        curl \
        gnupg \
        wget \
        ca-certificates \
        python3 \
        python3-pip \
        python3-venv \
        libgl1 \
        libglib2.0-0 \
        git && \
    rm -rf /var/lib/apt/lists/*

# Install Jellyfin (Official debuntu non-interactive installer)
RUN env DEBIAN_FRONTEND=noninteractive curl -fsSL https://repo.jellyfin.org/install-debuntu.sh | bash || true

# Set up clean isolated Python environment for PDF Enhancer
RUN python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir \
        httpx fastapi uvicorn python-multipart pydantic pymupdf opencv-python-headless numpy pillow || true

# Install PDF Enhancer service
RUN mkdir -p /opt/pdf_enhancer
COPY pdf_enhancer/ /opt/pdf_enhancer/

# Install Portal Dashboard
RUN mkdir -p /srv/portal
COPY portal/ /srv/portal/

# Install Apex Go Core Binary
RUN mkdir -p /opt/apex
COPY --from=go-builder /app/apex-core /opt/apex/apex-core
RUN chmod +x /opt/apex/apex-core 2>/dev/null || true

# Configuration & Gateway Setup
COPY nginx.conf /etc/nginx/nginx.conf
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Pre-create required directory layout
RUN mkdir -p /data/Stirling/configs \
             /data/Stirling/logs \
             /data/Stirling/customFiles \
             /data/Stirling/pipeline \
             /data/Stirling/storage \
             /data/Stirling/tessdata \
             /data/jellyfin/config \
             /data/jellyfin/cache \
             /data/jellyfin/media/Movies \
             /data/jellyfin/media/Shows \
             /data/apex/backups \
             /data/apex/session \
             /data/apex/metadata-cache \
             /tmp/stirling-pdf \
             /tmp/apex-db \
             /tmp/apex-stream-cache \
             /run/nginx

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
    JAVA_TOOL_OPTIONS="-Dstirling.base-path=/data/Stirling/ -XX:+ExitOnOutOfMemoryError -XX:+HeapDumpOnOutOfMemoryError -XX:HeapDumpPath=/data/Stirling/configs/heap_dumps -XX:+UseG1GC -XX:MaxGCPauseMillis=200 -Dspring.threads.virtual.enabled=true -Djava.awt.headless=true -XX:InitialRAMPercentage=10 -XX:MaxRAMPercentage=40 -XX:MaxMetaspaceSize=384m"

# Hugging Face default ingress port
EXPOSE 7860

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
