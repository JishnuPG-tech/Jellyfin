# Ultra-Lightweight Render 512MB RAM Dockerfile for OpenCode Serve Lite
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONMALLOC=malloc
ENV PYTHONUNBUFFERED=1
ENV MALLOC_TRIM_THRESHOLD_=65536
ENV PORT=10000

WORKDIR /app

# Install minimal system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git \
 && rm -rf /var/lib/apt/lists/*

# Install lightweight Python dependencies
RUN pip install --no-cache-dir \
      fastapi \
      "uvicorn[standard]" \
      "pydantic>=2.0" \
      python-dotenv \
      "huggingface_hub>=0.23"

# Copy consolidated single-process application files
COPY opencode_serve_lite.py /app/opencode_serve_lite.py
COPY sync_engine.py         /app/sync_engine.py
COPY README.md              /app/README.md

EXPOSE 10000

CMD ["python", "opencode_serve_lite.py"]
