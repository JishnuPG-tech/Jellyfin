# ==============================================================================
# Apex Media Platform - Production Dockerfile (v2.0 Architecture)
# Stack: Jellyfin 10.9.11 + Apex Go Core + Nginx Gateway (Port 7860)
# ==============================================================================

# ── Stage 1: Build Apex Go Core Daemon ─────────────────────────────────────────
FROM golang:1.22-bookworm AS go-builder

WORKDIR /app
COPY apex_src/ ./
RUN go mod tidy && CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o apex-core ./cmd/apex

# ── Stage 2: Main Production Image (Pinned Jellyfin LTS) ──────────────────────
FROM jellyfin/jellyfin:10.9.11

USER root

# Install Nginx, curl, and CA certificates
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        nginx \
        curl \
        ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Install Apex Core Go binary
RUN mkdir -p /opt/apex
COPY --from=go-builder /app/apex-core /opt/apex/apex-core
RUN chmod +x /opt/apex/apex-core

# Symlink Jellyfin binary to standard PATH if needed
RUN ln -sf /jellyfin/jellyfin /usr/local/bin/jellyfin 2>/dev/null || true

# Install Nginx Gateway configuration & Entrypoint
COPY nginx.conf /etc/nginx/nginx.conf
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Pre-create standard storage directories
RUN mkdir -p /data/jellyfin/data \
             /data/jellyfin/config \
             /data/jellyfin/backups \
             /data/jellyfin/log \
             /data/jellyfin/media/Movies \
             /data/jellyfin/media/Shows \
             /data/jellyfin/.aspnet/DataProtection-Keys \
             /data/apex/backups \
             /data/apex/session \
             /data/apex/metadata-cache \
             /tmp/jellyfin-cache \
             /tmp/apex-db \
             /tmp/apex-stream-cache

# Environment Configuration
ENV DATA_DIR="/data" \
    DOTNET_CLI_HOME="/data/jellyfin" \
    JELLYFIN_DATA_DIR="/data/jellyfin/data" \
    JELLYFIN_CONFIG_DIR="/data/jellyfin/config" \
    JELLYFIN_CACHE_DIR="/tmp/jellyfin-cache" \
    JELLYFIN_LOG_DIR="/data/jellyfin/log" \
    JELLYFIN_PORT="8096" \
    APEX_CORE_PORT="8084" \
    APEX_MEMORY_CACHE_MB="128" \
    APEX_DISK_CACHE_MB="2048" \
    APEX_PREFETCH_MB="16" \
    APEX_MAX_STREAMS="2" \
    APEX_TELEGRAM_MEDIA_CLIENTS="2"

# Hugging Face default ingress port
EXPOSE 7860

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
