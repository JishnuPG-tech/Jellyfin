# ==============================================================================
# Apex Multi-Project Cloud Space
# Unified Architecture: Stirling-PDF + SnapOtter + Caddy Gateway
# Optimized for Hugging Face Spaces (Persistent Storage + 16GB RAM)
# ==============================================================================

# Stage 1: Get clean, standalone Caddy binary
FROM caddy:2-alpine AS caddy-source

# Stage 2: Extract Stirling-PDF complete application and Java 25 runtime
FROM stirlingtools/stirling-pdf:latest AS stirling-source

# Stage 3: SnapOtter production runtime (Ubuntu 24.04 + Node 22 + Postgres 17 + Redis 8 + FFmpeg + AI)
FROM snapotter/snapotter:latest AS production

USER root

# Copy official Eclipse Temurin Java 25 JRE from Stirling-PDF
COPY --from=stirling-source /opt/java/openjdk /opt/java/openjdk

# Copy standalone Caddy binary
COPY --from=caddy-source /usr/bin/caddy /usr/local/bin/caddy
RUN chmod +x /usr/local/bin/caddy

# Set up Stirling-PDF complete layered application
RUN mkdir -p /stirling-app /tmp/stirling-pdf
COPY --from=stirling-source /app /stirling-app
COPY --from=stirling-source /scripts /stirling-scripts

# Portal goes to /srv/portal — OUTSIDE /app to avoid conflicts with SnapOtter's runtime
RUN mkdir -p /srv/portal
COPY portal/ /srv/portal/

# Caddyfile and entrypoint
COPY Caddyfile /etc/caddy/Caddyfile
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Pre-create persistent storage scaffolding at build time
RUN mkdir -p /data/Stirling/configs /data/Stirling/logs \
             /data/Stirling/customFiles /data/Stirling/pipeline \
             /data/Stirling/storage /data/files /data/logs \
             /data/redis /data/.home /tmp/workspace \
             /tmp/caddy/data /tmp/caddy/config

# Environment Configuration
ENV JAVA_HOME="/opt/java/openjdk" \
    PATH="/opt/java/openjdk/bin:${PATH}" \
    PORT="1349" \
    DATA_DIR="/data" \
    WORKSPACE_PATH="/tmp/workspace" \
    SYSTEM_ROOTURIPATH="/stirling" \
    STIRLING_BASE_PATH="/data/Stirling/" \
    CONFIG_FILE="/data/Stirling/configs/settings.yml" \
    STORAGE_LOCAL_BASEPATH="/data/Stirling/storage" \
    STIRLING_TEMPFILES_DIRECTORY="/tmp/stirling-pdf" \
    TMPDIR="/tmp/workspace" \
    TEMP="/tmp/workspace" \
    TMP="/tmp/workspace" \
    XDG_DATA_HOME="/tmp/caddy/data" \
    XDG_CONFIG_HOME="/tmp/caddy/config" \
    JAVA_TOOL_OPTIONS="-Dstirling.base-path=/data/Stirling/ -XX:+ExitOnOutOfMemoryError -XX:+HeapDumpOnOutOfMemoryError -XX:HeapDumpPath=/data/Stirling/configs/heap_dumps -XX:+UseG1GC -XX:MaxGCPauseMillis=200 -Dspring.threads.virtual.enabled=true -Djava.awt.headless=true -XX:InitialRAMPercentage=5 -XX:MaxRAMPercentage=25 -XX:MaxMetaspaceSize=256m"

# Hugging Face default container port
EXPOSE 7860

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
