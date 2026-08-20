# 🛠️ Apex Multi-Tool Cloud Suite — Complete System & Troubleshooting Guide

**Repository / Space**: [Hugging Face Space: Jishnupg/Apex](https://huggingface.co/spaces/Jishnupg/Apex)  
**Architecture**: Caddy Reverse Proxy Gateway + Stirling-PDF + Static Portal Hub  
**Environment**: Hugging Face Spaces (Docker, Persistent Storage at `/data`)

---

## 1. 🏗️ Architecture Overview

The Apex Space is designed as a **modular multi-tool cloud suite** running inside a single container using a Caddy reverse proxy to unify all internal services under port `7860`.

```
                    Internet / HF Space URL
                              │
                              ▼
        ┌──────────────────────────────────────────┐
        │       Caddy Gateway (:7860 Public)       │
        └─────────────────────┬────────────────────┘
                              │
               ┌──────────────┴──────────────┐
               ▼                             ▼
       /stirling* (Proxy)             / (Static Files)
               │                             │
               ▼                             ▼
    ┌───────────────────────┐   ┌───────────────────────────┐
    │  Stirling-PDF (:8080) │   │     Portal Hub UI         │
    │   (Java 25 + OCR)     │   │     (/srv/portal)         │
    │ (H2 Embedded Storage) │   │ (Interactive Dashboard)   │
    └───────────────────────┘   └───────────────────────────┘
               │
               ▼
      /data/Stirling/
    (Persistent Storage)
```

---

## 2. 📁 Persistent Storage Layout

All state, credentials, configurations, and pipeline workflows are stored in the persistent volume mounted at `/data/`:

```
/data/Stirling/
├── configs/          # User accounts, passwords, embedded H2 database, settings.yml
├── logs/             # Application and server logs
├── customFiles/      # Custom fonts, digital signatures, certificates
├── pipeline/         # Saved automated conversion pipelines & workflows
├── storage/          # Temporary & persistent file storage
└── tessdata/         # Tesseract OCR language training data (.traineddata)
```

---

## 3. 🔍 Bugs Encountered & Root Causes (Historical Record)

### 🔴 1. The PostgreSQL + SnapOtter Problem on Hugging Face Spaces
* **Symptoms**:
  * Persistent volume `fsync` stalls (50–90 seconds every startup)
  * `FATAL: the database system is starting up`
  * `could not open file "postmaster.pid": No such file or directory` → emergency shutdown
  * `FATAL: database "snapotter" does not exist`
  * Infinite log spam: `Unable to acquire the AI install lock: EACCES: permission denied, open '/data/ai/install.flock'`
* **Root Cause**:
  * Running a standalone PostgreSQL daemon on a virtualized/network-attached persistent volume (`/data`) causes I/O latency, lock file invalidation, and slow recovery checks.
  * SnapOtter required Node.js 22 + TypeScript `tsx` runtime + Redis + PostgreSQL + Python AI models. This heavy multi-layer stack led to memory contention and frequent crashloops.
* **Resolution**:
  * Removed PostgreSQL, Redis, and SnapOtter entirely.
  * Replaced with official `stirlingtools/stirling-pdf:latest` which uses a fast, lightweight **embedded H2 file-based storage** system.

### 🔴 2. Security Vulnerability: Static Path Traversal Exposing `.env`
* **Symptoms**:
  * Requests like `GET /.env` and `GET /file=../../.env` returned HTTP 200 with sensitive configuration details.
* **Root Cause**:
  * Static file handlers and wildcard reverse proxy catch-alls served files from parent/root directories without path filtering.
* **Resolution**:
  * Added strict Caddy routing and removed unrestricted upstream file routes.

### 🔴 3. Premature Zero-Exit Shutdown in Container Lifecycle
* **Symptoms**:
  * Container immediately shut down on startup with `Exit code: 0`.
* **Root Cause**:
  * The process supervisor loop polled `CADDY_PID` and `STIRLING_PID` before background Java initialization had fully stabilized, triggering the cleanup trap.
* **Resolution**:
  * Added stabilization delay (`sleep 3`) and dynamic jar location resolution in `entrypoint.sh`.

---

## 4. ⚙️ Key Configuration Files

### `Dockerfile`
```dockerfile
FROM caddy:2-alpine AS caddy-source
FROM stirlingtools/stirling-pdf:latest

USER root

# Install Caddy
COPY --from=caddy-source /usr/bin/caddy /usr/local/bin/caddy
RUN chmod +x /usr/local/bin/caddy

# Portal Dashboard & Gateway
RUN mkdir -p /srv/portal
COPY portal/ /srv/portal/
COPY Caddyfile /etc/caddy/Caddyfile
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Pre-create folders
RUN mkdir -p /data/Stirling/configs /data/Stirling/logs /data/Stirling/customFiles \
             /data/Stirling/pipeline /data/Stirling/storage /data/Stirling/tessdata \
             /tmp/stirling-pdf /tmp/caddy/data /tmp/caddy/config

ENV PORT="8080" \
    DATA_DIR="/data" \
    SYSTEM_ROOTURIPATH="/stirling" \
    STIRLING_BASE_PATH="/data/Stirling/" \
    CONFIG_FILE="/data/Stirling/configs/settings.yml" \
    STORAGE_LOCAL_BASEPATH="/data/Stirling/storage" \
    STIRLING_TEMPFILES_DIRECTORY="/tmp/stirling-pdf" \
    XDG_DATA_HOME="/tmp/caddy/data" \
    XDG_CONFIG_HOME="/tmp/caddy/config" \
    JAVA_TOOL_OPTIONS="-Dstirling.base-path=/data/Stirling/ -XX:+ExitOnOutOfMemoryError -XX:+HeapDumpOnOutOfMemoryError -XX:HeapDumpPath=/data/Stirling/configs/heap_dumps -XX:+UseG1GC -XX:MaxGCPauseMillis=200 -Dspring.threads.virtual.enabled=true -Djava.awt.headless=true -XX:InitialRAMPercentage=10 -XX:MaxRAMPercentage=60 -XX:MaxMetaspaceSize=384m"

EXPOSE 7860
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
```

### `Caddyfile`
```caddy
:7860 {
	# ── 1. Stirling-PDF Proxy ──────────────────────────────────────────────────
	handle /stirling* {
		reverse_proxy 127.0.0.1:8080
	}

	# ── 2. Apex Portal Hub (Static Web UI) ────────────────────────────────────
	handle {
		root * /srv/portal
		file_server {
			index index.html
		}
	}
}
```

---

## 5. ➕ How to Add New Tools in Future

To add a new tool (e.g. IT-Tools, FileBrowser, or custom microservice on port `8081`):

1. **Install tool binary/assets** in `Dockerfile`.
2. **Start the background process** in `entrypoint.sh`:
   ```bash
   /path/to/mytool --port 8081 &
   TOOL_PID=$!
   ```
3. **Add the route in `Caddyfile`**:
   ```caddy
   handle /mytool* {
       reverse_proxy 127.0.0.1:8081
   }
   ```
4. **Update `portal/index.html`** to link to `/mytool/`.
