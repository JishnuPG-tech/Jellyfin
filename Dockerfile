## OpenCode-Serve · production mode
## nginx on :7860 (HF exposed) proxies:
##   /terminal → ttyd on :7681 (real PTY bash, mobile-optimised)
##   /         → opencode on :8080 (chat UI + REST API + SSE)
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

# XDG dirs — local persistent paths.
ENV XDG_DATA_HOME=/data/share
ENV XDG_CONFIG_HOME=/data/config
ENV XDG_CACHE_HOME=/data/cache
ENV XDG_STATE_HOME=/data/state

ENV PORT=7860

ARG OPENCODE_VERSION=1.18.3
ARG TTYD_VERSION=1.7.7

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git gnupg python3 python3-pip nginx \
 && curl -fsSL "https://github.com/anomalyco/opencode/releases/download/v${OPENCODE_VERSION}/opencode-linux-x64.tar.gz" \
      | tar -xz -C /usr/local/bin opencode \
 && chmod +x /usr/local/bin/opencode \
 && curl -fsSL "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.x86_64" \
      -o /usr/local/bin/ttyd \
 && chmod +x /usr/local/bin/ttyd \
 && rm -rf /var/lib/apt/lists/*

# Install huggingface_hub for sync engine
RUN pip3 install --quiet --no-cache-dir --break-system-packages "huggingface_hub>=0.23"

# Pre-create /data dirs
RUN mkdir -p \
      /data/share/opencode \
      /data/config/opencode \
      /data/cache/opencode \
      /data/state/opencode \
      /data/logs \
      /data/workspaces \
      /projects/default

COPY cleaner.py         /cleaner.py
COPY sync_engine.py     /sync_engine.py
COPY memory_updater.py  /memory_updater.py
COPY session_watcher.py /session_watcher.py
COPY memctl.py          /memctl.py
COPY entrypoint.sh      /entrypoint.sh
RUN chmod +x /entrypoint.sh /sync_engine.py /memory_updater.py /session_watcher.py /memctl.py \
 && ln -sf /memctl.py /usr/local/bin/memctl \
 && rm -f /etc/nginx/sites-enabled/default

WORKDIR /projects/default

EXPOSE 7860

ENTRYPOINT ["/entrypoint.sh"]
