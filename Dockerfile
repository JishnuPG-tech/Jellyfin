# ==============================================================================
# Apex Multi-Project Cloud Space (Stirling-PDF + Microservices)
# Optimized for Hugging Face Spaces Free Tier (2 vCPU, 16 GB RAM, 50 GB Disk)
# ==============================================================================

FROM stirlingtools/stirling-pdf:latest

USER root

# Install Nginx (reverse proxy gateway) and Supervisor (multi-process manager)
RUN apt-get update && apt-get install -y --no-install-recommends \
    nginx \
    supervisor \
    && rm -rf /var/lib/apt/lists/*

# Create application portal directories & working directories
WORKDIR /app
RUN mkdir -p /app/portal \
    /tmp/stirling-pdf \
    /tmp/stirling-pdf/heap_dumps \
    /tmp/nginx_client_body \
    /tmp/nginx_proxy \
    /tmp/nginx_fastcgi \
    /tmp/nginx_uwsgi \
    /tmp/nginx_scgi \
    /configs /logs /customFiles /pipeline /storage

# Copy Gateway and Portal configuration
COPY nginx.conf /app/nginx.conf
COPY supervisord.conf /app/supervisord.conf
COPY portal/ /app/portal/

# Set up non-root permissions for Hugging Face Spaces (UID 1000)
RUN chown -R stirlingpdfuser:stirlingpdfgroup /app /tmp /configs /logs /customFiles /pipeline /storage /etc/nginx /var/log/nginx /var/lib/nginx \
    && chmod -R 777 /tmp \
    && chmod -R 755 /app

# Switch to non-root user (UID 1000 required by HF Spaces)
USER stirlingpdfuser

# Environment variables for Stirling-PDF and HF Spaces
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
    TMP="/tmp/stirling-pdf"

# Hugging Face default container port
EXPOSE 7860

# Start Supervisor to run both Nginx (Port 7860) and Stirling-PDF (Port 8080)
CMD ["supervisord", "-c", "/app/supervisord.conf"]
