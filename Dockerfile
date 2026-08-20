# ==============================================================================
# Apex Multi-Project Cloud Space
# Architecture: Stirling-PDF + Gemini Web2API + Caddy Gateway + Portal Hub
# Optimized for Hugging Face Spaces (Persistent Storage + Fast Boot)
# ==============================================================================

# Stage 1: Clean Caddy binary
FROM caddy:2-alpine AS caddy-source

# Stage 2: Official Stirling-PDF (Complete PDF suite)
FROM stirlingtools/stirling-pdf:latest

USER root

# Install Caddy Gateway
COPY --from=caddy-source /usr/bin/caddy /usr/local/bin/caddy
RUN chmod +x /usr/local/bin/caddy

# Set up clean isolated Python environment for Gemini Web2API
RUN apt-get update && \
    apt-get install -y --no-install-recommends python3 python3-pip python3-venv python3-httpx || true && \
    python3 -m venv /opt/gemini_venv && \
    /opt/gemini_venv/bin/pip install --no-cache-dir httpx || true && \
    rm -rf /var/lib/apt/lists/*

# Install Gemini Web2API service
RUN mkdir -p /opt/gemini_web2api /etc/gemini_web2api /data/gemini
COPY gemini_web2api/ /opt/gemini_web2api/gemini_web2api/
COPY gemini_config.json /etc/gemini_web2api/config.json

# Portal Dashboard
RUN mkdir -p /srv/portal
COPY portal/ /srv/portal/

# Gateway & Entrypoint
COPY Caddyfile /etc/caddy/Caddyfile
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Pre-create persistent and temp folders
RUN mkdir -p /data/Stirling/configs \
             /data/Stirling/logs \
             /data/Stirling/customFiles \
             /data/Stirling/pipeline \
             /data/Stirling/storage \
             /data/Stirling/tessdata \
             /data/gemini \
             /tmp/stirling-pdf \
             /tmp/caddy/data \
             /tmp/caddy/config

# Environment Configuration
ENV PORT="8080" \
    DATA_DIR="/data" \
    SYSTEM_ROOTURIPATH="/stirling" \
    STIRLING_BASE_PATH="/data/Stirling/" \
    CONFIG_FILE="/data/Stirling/configs/settings.yml" \
    STORAGE_LOCAL_BASEPATH="/data/Stirling/storage" \
    STIRLING_TEMPFILES_DIRECTORY="/tmp/stirling-pdf" \
    GEMINI_PORT="8081" \
    GEMINI_CONFIG="/data/gemini/config.json" \
    XDG_DATA_HOME="/tmp/caddy/data" \
    XDG_CONFIG_HOME="/tmp/caddy/config" \
    JAVA_TOOL_OPTIONS="-Dstirling.base-path=/data/Stirling/ -XX:+ExitOnOutOfMemoryError -XX:+HeapDumpOnOutOfMemoryError -XX:HeapDumpPath=/data/Stirling/configs/heap_dumps -XX:+UseG1GC -XX:MaxGCPauseMillis=200 -Dspring.threads.virtual.enabled=true -Djava.awt.headless=true -XX:InitialRAMPercentage=10 -XX:MaxRAMPercentage=50 -XX:MaxMetaspaceSize=384m"

# Hugging Face Spaces default port
EXPOSE 7860

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
