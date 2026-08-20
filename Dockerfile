# ==============================================================================
# Apex Multi-Project Cloud Space (Stirling-PDF + Microservices Gateway)
# Optimized for Hugging Face Spaces Free Tier (2 vCPU, 16 GB RAM, 50 GB Disk)
# ==============================================================================

# Stage 1: Get clean, standalone Caddy binary (no package manager / apt dependencies)
FROM caddy:2-alpine AS caddy-source

# Stage 2: Main Stirling-PDF runtime
FROM stirlingtools/stirling-pdf:latest

USER root

# Copy standalone Caddy gateway binary
COPY --from=caddy-source /usr/bin/caddy /usr/local/bin/caddy
RUN chmod +x /usr/local/bin/caddy

# Set up working directory & storage
WORKDIR /app
RUN mkdir -p /app/portal \
    /tmp/caddy/data \
    /tmp/caddy/config \
    /tmp/stirling-pdf \
    /tmp/stirling-pdf/heap_dumps \
    /configs /logs /customFiles /pipeline /storage

# Copy Gateway and Portal configuration
COPY Caddyfile /app/Caddyfile
COPY entrypoint.sh /app/entrypoint.sh
COPY portal/ /app/portal/

# Set up permissions for Hugging Face Spaces non-root execution (UID 1000)
RUN chmod +x /app/entrypoint.sh \
    && chown -R stirlingpdfuser:stirlingpdfgroup /app /tmp /configs /logs /customFiles /pipeline /storage \
    && chmod -R 777 /tmp \
    && chmod -R 755 /app

# Switch to non-root user (UID 1000)
USER stirlingpdfuser

# Environment variables
ENV SYSTEM_ROOTURIPATH="/stirling" \
    SERVER_PORT="8080" \
    HOME="/home/stirlingpdfuser" \
    PUID=1000 \
    PGID=1000 \
    UMASK=022 \
    DISABLE_ADDITIONAL_FEATURES="false" \
    DOCKER_ENABLE_SECURITY="false" \
    STIRLING_TEMPFILES_DIRECTORY="/tmp/stirling-pdf" \
    TMPDIR="/tmp/stirling-pdf" \
    TEMP="/tmp/stirling-pdf" \
    TMP="/tmp/stirling-pdf" \
    XDG_DATA_HOME="/tmp/caddy/data" \
    XDG_CONFIG_HOME="/tmp/caddy/config"

# Hugging Face default container port
EXPOSE 7860

# Run multi-service entrypoint
ENTRYPOINT ["/app/entrypoint.sh"]
