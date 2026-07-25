## OpenCode-Serve · proxy mode
## Gateway (uvicorn) on :7860, opencode upstream on :4096, ttyd on :7681.
## /terminal/*  -> ttyd
## /           -> redirect HTML (auto-load latest session)
## /*          -> opencode serve (passthrough)
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

ENV XDG_DATA_HOME=/data/share
ENV XDG_CONFIG_HOME=/data/config
ENV XDG_CACHE_HOME=/data/cache
ENV XDG_STATE_HOME=/data/state

ENV PORT=7860
ENV OPENCODE_PORT=4096
ENV TTYD_PORT=7681

ARG OPENCODE_VERSION=1.18.3
ARG TTYD_VERSION=1.7.7

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git gnupg python3 python3-pip \
 && pip3 install --break-system-packages fastapi uvicorn httpx websockets psutil \
 && curl -fsSL "https://github.com/anomalyco/opencode/releases/download/v${OPENCODE_VERSION}/opencode-linux-x64.tar.gz" \
      | tar -xz -C /usr/local/bin opencode \
 && chmod +x /usr/local/bin/opencode \
 && curl -fsSL "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.x86_64" \
      -o /usr/local/bin/ttyd \
 && chmod +x /usr/local/bin/ttyd \
 && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /projects/default

COPY backend/ /app/backend/
COPY cleaner.py /cleaner.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /app

EXPOSE 7860

ENTRYPOINT ["/entrypoint.sh"]
