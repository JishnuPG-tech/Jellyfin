## OpenCode-Serve · production-ready embedded terminal
##
## Single-container image:
##   * opencode-serve   (Python FastAPI on :7860 — what HF exposes)
##   * opencode         (upstream Go binary on :4096 — internal only)
##   * Persistent /bin/bash PTY inside the same container (no extra ttyd)
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Persistent dataset bucket mounted at /data (see HF Spaces docs).
ENV XDG_DATA_HOME=/data/share
ENV XDG_CONFIG_HOME=/data/config
ENV XDG_CACHE_HOME=/data/cache
ENV XDG_STATE_HOME=/data/state
ENV WORKDIR=/data/workspaces/default

# Where this FastAPI listens (also HF's `app_port`).
ENV PORT=7860

ARG OPENCODE_VERSION=1.18.3

# ────────────────────────────────────────────────────────────────────────
# System packages
#   * git      — entrypoint clones the configured project repo
#   * python3  — backend + SQLite-cleaner daemon
#   * procps   — needed for `kill -0 pgrp` style cleanups
#   * bash     — default; no install needed in bookworm-slim (already there)
# ────────────────────────────────────────────────────────────────────────
RUN apt-get update \
  && apt-get install -y --no-install-recommends \
       ca-certificates curl git gnupg python3 python3-pip procps \
  && rm -rf /var/lib/apt/lists/*

# Opencode upstream binary — the AI server. Bound to 127.0.0.1; the
# FastAPI proxy is the only thing exposed to the network.
RUN curl -fsSL \
      "https://github.com/anomalyco/opencode/releases/download/v${OPENCODE_VERSION}/opencode-linux-x64.tar.gz" \
      | tar -xz -C /usr/local/bin opencode \
  && chmod +x /usr/local/bin/opencode

# Application directory.
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt

# Copy application sources. .dockerignore keeps tests + .git out.
COPY backend   /app/backend
COPY cleaner.py  /cleaner.py
COPY entrypoint.sh /entrypoint.sh

RUN mkdir -p /data/workspaces /data/bin /data/logs \
  && chmod +x /entrypoint.sh

EXPOSE 7860

# Self-healing SQLite daemon is started by the entrypoint.
# `exec uvicorn` so signals reach the worker.
ENTRYPOINT ["/entrypoint.sh"]
