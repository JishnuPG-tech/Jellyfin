# ==============================================================================
# Apex Multi-Project Cloud Space (Stirling-PDF + Microservices Gateway)
# Optimized for Hugging Face Spaces (Persistent Storage + Free Tier)
# ==============================================================================

# Stage 1: Get clean, standalone Caddy binary
FROM caddy:2-alpine AS caddy-source

# Stage 2: Main Stirling-PDF runtime
FROM stirlingtools/stirling-pdf:latest

USER root

# Copy standalone Caddy gateway binary
COPY --from=caddy-source /usr/bin/caddy /usr/local/bin/caddy
RUN chmod +x /usr/local/bin/caddy

# Set up working directory, persistent storage root & system directories
WORKDIR /app
RUN mkdir -p /app/portal \
    /data/Stirling/configs \
    /data/Stirling/logs \
    /data/Stirling/customFiles \
    /data/Stirling/pipeline \
    /data/Stirling/storage \
    /data/Stirling/tessdata \
    /home/stirlingpdfuser \
    /tmp/caddy/data \
    /tmp/caddy/config \
    /tmp/stirling-pdf \
    /tmp/stirling-pdf/heap_dumps \
    /tmp/stirling-pdf/libre \
    /configs /logs /customFiles /pipeline /storage \
    /usr/share/tessdata /usr/share/tesseract-ocr/5/tessdata \
    /usr/local/bin

# Pre-create Stirling diagnostic symlinks during build
RUN if [ -f /scripts/stirling-diagnostics.sh ]; then \
        ln -sf /scripts/stirling-diagnostics.sh /usr/local/bin/diagnostics && \
        ln -sf /scripts/stirling-diagnostics.sh /usr/local/bin/stirling-diagnostics && \
        ln -sf /scripts/stirling-diagnostics.sh /usr/local/bin/diag && \
        ln -sf /scripts/stirling-diagnostics.sh /usr/local/bin/debug && \
        ln -sf /scripts/stirling-diagnostics.sh /usr/local/bin/diagnostic; \
    fi

# Copy Gateway and Portal configuration
COPY Caddyfile /app/Caddyfile
COPY entrypoint.sh /app/entrypoint.sh
COPY portal/ /app/portal/

# Set up non-root permissions for Hugging Face Spaces (UID 1000)
RUN chmod +x /app/entrypoint.sh \
    && chown -R stirlingpdfuser:stirlingpdfgroup \
        /app \
        /data \
        /home/stirlingpdfuser \
        /tmp \
        /configs \
        /logs \
        /customFiles \
        /pipeline \
        /storage \
        /usr/share/tessdata \
        /usr/share/tesseract-ocr \
        /usr/local/bin \
        /scripts \
    && chmod -R 777 /tmp \
    && chmod -R 777 /usr/local/bin \
    && chmod -R 775 /data \
    && chmod -R 755 /app /configs /logs /customFiles /pipeline /storage

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
    CONFIG_FILE="/data/Stirling/configs/settings.yml" \
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
