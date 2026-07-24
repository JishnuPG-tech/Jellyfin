## OpenCode-Serve · production-ready embedded terminal
##
## Runs *one* process on the HF-exposed port :7860:
##   - a thin Python gateway (uvicorn) that proxies everything by byte
##   - upstream opencode serve on :4096 (internal)
##   - upstream ttyd         on :7681 (internal)
##   - persistent /bin/bash PTY inside the same container
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV PYTHONUNBUFFERED=1

ENV XDG_DATA_HOME=/data/share
ENV XDG_CONFIG_HOME=/data/config
ENV XDG_CACHE_HOME=/data/cache
ENV XDG_STATE_HOME=/data/state

ENV PORT=7860
ENV OPENCODE_PORT=4096
ENV TTYD_PORT=7681

ARG OPENCODE_VERSION=1.18.3

RUN apt-get update && apt-get install -y --no-install-recommends \
       ca-certificates curl git gnupg python3 python3-pip ttyd \
 && curl -fsSL "https://github.com/anomalyco/opencode/releases/download/v${OPENCODE_VERSION}/opencode-linux-x64.tar.gz" \
      | tar -xz -C /usr/local/bin opencode \
 && chmod +x /usr/local/bin/opencode \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt

COPY backend /app/backend
COPY cleaner.py /cleaner.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

RUN mkdir -p /data/workspaces /data/logs /projects/default

EXPOSE 7860

ENTRYPOINT ["/entrypoint.sh"]
