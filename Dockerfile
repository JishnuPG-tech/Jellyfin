FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV WORKSPACE_PATH=/data/workspaces
ENV LOG_LEVEL=INFO
ENV TTYD_PORT=7681

RUN apt-get update && apt-get install -y --no-install-recommends \
    tmux \
    curl \
    ca-certificates \
    supervisor \
    unzip \
    git \
  && curl -fsSL https://github.com/anomalyco/opencode/releases/download/v1.18.3/opencode-linux-x64.tar.gz -o /tmp/opencode.tar.gz \
  && tar -xzf /tmp/opencode.tar.gz -C /usr/bin/ \
  && chmod +x /usr/bin/opencode \
  && rm -f /tmp/opencode.tar.gz \
  && curl -fsSL https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.x86_64 -o /usr/local/bin/ttyd \
  && chmod +x /usr/local/bin/ttyd \
  && rm -rf /var/lib/apt/lists/*

RUN useradd -m -s /bin/bash appuser \
 && mkdir -p /data/workspaces /data/bin /data/logs \
 && chown -R appuser:appuser /data

WORKDIR /app

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app
RUN chmod +x /app/scripts/*.sh || true

EXPOSE 7860 7681

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
