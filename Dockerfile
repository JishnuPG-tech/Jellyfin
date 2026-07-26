## OpenCode-Serve · direct mode + terminal
## nginx on :7860 (HF exposed) proxies:
##   /terminal → ttyd on :7681 (real PTY bash)
##   /         → opencode on :8080 (chat UI + API)
##
## SSH access: sshd on :22 (internal) reached via bore transparent TCP tunnel.
## bore.pub:PORT → raw TCP → sshd:22  (no SSH interception → full PTY support)
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

# XDG dirs — now local (no /data volume required).
# sync_engine.py restores these from the HF Dataset on startup.
ENV XDG_DATA_HOME=/data/share
ENV XDG_CONFIG_HOME=/data/config
ENV XDG_CACHE_HOME=/data/cache
ENV XDG_STATE_HOME=/data/state

ENV PORT=7860

ARG OPENCODE_VERSION=1.18.3
ARG TTYD_VERSION=1.7.7
ARG BORE_VERSION=0.6.0

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git gnupg python3 python3-pip nginx \
      openssh-server \
 && curl -fsSL "https://github.com/anomalyco/opencode/releases/download/v${OPENCODE_VERSION}/opencode-linux-x64.tar.gz" \
      | tar -xz -C /usr/local/bin opencode \
 && chmod +x /usr/local/bin/opencode \
 && curl -fsSL "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.x86_64" \
      -o /usr/local/bin/ttyd \
 && chmod +x /usr/local/bin/ttyd \
 && curl -fsSL "https://github.com/ekzhang/bore/releases/download/v${BORE_VERSION}/bore-v${BORE_VERSION}-x86_64-unknown-linux-musl.tar.gz" \
      | tar -xz -C /usr/local/bin bore \
 && chmod +x /usr/local/bin/bore \
 && mkdir -p /var/run/sshd \
 && rm -rf /var/lib/apt/lists/*

# Install huggingface_hub for the sync engine
RUN pip3 install --quiet --no-cache-dir --break-system-packages "huggingface_hub>=0.23"

# Pre-create /data dirs so XDG paths always exist even without a volume mount.
# sync_engine.py will restore their contents from the HF Dataset at startup.
RUN mkdir -p \
      /data/share/opencode \
      /data/config/opencode \
      /data/cache/opencode \
      /data/state/opencode \
      /data/logs \
      /data/workspaces \
      /projects/default

COPY cleaner.py        /cleaner.py
COPY sync_engine.py    /sync_engine.py
COPY memory_updater.py /memory_updater.py
COPY entrypoint.sh     /entrypoint.sh
RUN chmod +x /entrypoint.sh /sync_engine.py /memory_updater.py \
 && rm -f /etc/nginx/sites-enabled/default

WORKDIR /projects/default

EXPOSE 7860

ENTRYPOINT ["/entrypoint.sh"]
