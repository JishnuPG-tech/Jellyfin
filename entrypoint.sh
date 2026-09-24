#!/bin/sh
set -e

START_TIME=$(date +%s)
get_elapsed() {
    if [ -n "$START_TIME" ]; then
        python3 -c "import time; print(f'{time.time() - $START_TIME:.2f}s')" 2>/dev/null || echo "0.0s"
    else
        echo "0.0s"
    fi
}

log_info() {
    echo "$(date -u '+%Y-%m-%d %H:%M:%S') [INFO] [$1] $2"
}

log_warn() {
    echo "$(date -u '+%Y-%m-%d %H:%M:%S') [WARN] [$1] $2"
}

log_error() {
    echo "$(date -u '+%Y-%m-%d %H:%M:%S') [ERROR] [$1] $2"
}

log_info "SYSTEM" "============================================"
log_info "SYSTEM" "=== OpenCode Space Container Starting ==="
log_info "SYSTEM" "============================================"

# Step 1: Configure Git & Environment Secrets Validation
git config --global --add safe.directory '*' 2>/dev/null || true

# Step 2: Ensure persistent /data directory structure exists
log_info "INIT" "Setting up /data persistent volume directories..."
mkdir -p /data/share/opencode /data/config/opencode /data/cache/opencode /data/state/opencode 2>/dev/null || true
mkdir -p /data/omniroute 2>/dev/null || true
mkdir -p /data/jellyfin/data /data/jellyfin/config /data/jellyfin/cache /data/jellyfin/log /data/jellyfin/media/Movies /data/jellyfin/media/TVShows 2>/dev/null || true
mkdir -p /data/hermes/memories /data/hermes/skills /data/hermes/sessions 2>/dev/null || true
mkdir -p /root/.cache /data/cache /root/.hermes/memories /root/.hermes/skills 2>/dev/null || true
chmod 777 /root/.cache /data/cache /data/hermes /data/omniroute 2>/dev/null || true

# Playwright Browser Metadata Fix for gemini-web / browser providers
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
export PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright
mkdir -p /root/.cache/ms-playwright /omniroute/.cache/ms-playwright 2>/dev/null || true
python3 -c "
import os, json
browsers_json = {
  'browsers': [
    {'name': 'chromium', 'revision': '1112', 'installByDefault': True},
    {'name': 'firefox', 'revision': '1448', 'installByDefault': True},
    {'name': 'webkit', 'revision': '2007', 'installByDefault': True},
    {'name': 'ffmpeg', 'revision': '1009', 'installByDefault': True}
  ]
}
targets = [
    '/root/.cache/ms-playwright',
    '/omniroute/.cache/ms-playwright',
    '/omniroute/node_modules/playwright-core',
    '/omniroute/node_modules/playwright/node_modules/playwright-core',
    '/usr/local/lib/node_modules/playwright-core',
    '/usr/local/lib/node_modules/playwright/node_modules/playwright-core'
]
if os.path.exists('/omniroute'):
    for root, dirs, files in os.walk('/omniroute'):
        if os.path.basename(root) == 'playwright-core':
            targets.append(root)

for target in set(targets):
    try:
        os.makedirs(target, exist_ok=True)
        b_path = os.path.join(target, 'browsers.json')
        if not os.path.exists(b_path) or os.path.getsize(b_path) == 0:
            with open(b_path, 'w', encoding='utf-8') as f:
                json.dump(browsers_json, f, indent=2)
            print(f'[PLAYWRIGHT] Created browsers.json at {b_path}')
    except Exception:
        pass
" 2>/dev/null || true

# ── STEP 1: Master Secret Validation & Persistent Encryption Keys ─────────────
mkdir -p /data/omniroute 2>/dev/null || true

if [ -n "$STORAGE_ENCRYPTION_KEY" ]; then
    echo "$STORAGE_ENCRYPTION_KEY" > /data/omniroute/.storage_encryption_key 2>/dev/null || true
elif [ -f "/data/omniroute/.storage_encryption_key" ] && [ -s "/data/omniroute/.storage_encryption_key" ]; then
    export STORAGE_ENCRYPTION_KEY=$(cat /data/omniroute/.storage_encryption_key | tr -d '\r\n')
    echo "[PERSISTENCE] Loaded persistent STORAGE_ENCRYPTION_KEY from /data/omniroute/.storage_encryption_key"
else
    export STORAGE_ENCRYPTION_KEY=$(python3 -c "import secrets; print(secrets.token_hex(16))" 2>/dev/null || echo "omniroute_default_storage_key_32bytes")
    echo "$STORAGE_ENCRYPTION_KEY" > /data/omniroute/.storage_encryption_key 2>/dev/null || true
    echo "[PERSISTENCE] Generated and saved persistent STORAGE_ENCRYPTION_KEY to /data/omniroute"
fi

if [ -n "$JWT_SECRET" ]; then
    echo "$JWT_SECRET" > /data/omniroute/.jwt_secret 2>/dev/null || true
elif [ -f "/data/omniroute/.jwt_secret" ] && [ -s "/data/omniroute/.jwt_secret" ]; then
    export JWT_SECRET=$(cat /data/omniroute/.jwt_secret | tr -d '\r\n')
    echo "[PERSISTENCE] Loaded persistent JWT_SECRET from /data/omniroute/.jwt_secret"
else
    export JWT_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(16))" 2>/dev/null || echo "omniroute_default_jwt_secret_key_32bytes")
    echo "$JWT_SECRET" > /data/omniroute/.jwt_secret 2>/dev/null || true
    echo "[PERSISTENCE] Generated and saved persistent JWT_SECRET to /data/omniroute"
fi

if [ -n "$API_KEY_SECRET" ]; then
    echo "$API_KEY_SECRET" > /data/omniroute/.api_key_secret 2>/dev/null || true
elif [ -f "/data/omniroute/.api_key_secret" ] && [ -s "/data/omniroute/.api_key_secret" ]; then
    export API_KEY_SECRET=$(cat /data/omniroute/.api_key_secret | tr -d '\r\n')
    echo "[PERSISTENCE] Loaded persistent API_KEY_SECRET from /data/omniroute/.api_key_secret"
else
    export API_KEY_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(16))" 2>/dev/null || echo "omniroute_default_api_key_secret_32bytes")
    echo "$API_KEY_SECRET" > /data/omniroute/.api_key_secret 2>/dev/null || true
    echo "[PERSISTENCE] Generated and saved persistent API_KEY_SECRET to /data/omniroute"
fi

if [ -z "$INITIAL_PASSWORD" ]; then
    if [ -f "/data/omniroute/.initial_password" ] && [ -s "/data/omniroute/.initial_password" ]; then
        export INITIAL_PASSWORD=$(cat /data/omniroute/.initial_password | tr -d '\r\n')
    else
        export INITIAL_PASSWORD="admin123"
        echo "$INITIAL_PASSWORD" > /data/omniroute/.initial_password 2>/dev/null || true
    fi
else
    echo "$INITIAL_PASSWORD" > /data/omniroute/.initial_password 2>/dev/null || true
fi

export ENCRYPTION_SECRET="${STORAGE_ENCRYPTION_KEY}"
export OMNIROUTE_SECRET_KEY="${STORAGE_ENCRYPTION_KEY}"
export OMNIROUTE_STORAGE_KEY="${STORAGE_ENCRYPTION_KEY}"
export OMNIROUTE_STORAGE_SECRET="${STORAGE_ENCRYPTION_KEY}"
export OMNIROUTE_WS_BRIDGE_SECRET="${OMNIROUTE_WS_BRIDGE_SECRET:-$(echo "ws_bridge_${JWT_SECRET}" | sha256sum | cut -c1-48)}"

# Purge any legacy secret env files from storage bucket to maintain strict secret separation
rm -f /data/.env /data/secrets.env /data/secrets /data/config/.env /data/omniroute/.env /data/omniroute/secrets.env /data/omniroute/secrets /data/omniroute/.secrets /data/omniroute/server.env 2>/dev/null || true

echo "[BOOT] Secrets validated: $(get_elapsed)"

# ── STEP 2: Directory Model & SQLite Snapshot Restoration ─────────────────────
PERSIST_DIR="/data/omniroute"
PERSIST_DB="/data/omniroute/storage.sqlite"
BACKUP_DIR="/data/omniroute/backups"

RUNTIME_DIR="/root/.omniroute"
RUNTIME_DB="/root/.omniroute/storage.sqlite"

if [ -d "$PERSIST_DB" ]; then
    rm -rf "$PERSIST_DB" 2>/dev/null || true
fi

mkdir -p "$PERSIST_DIR" "$BACKUP_DIR" "$RUNTIME_DIR" 2>/dev/null || true

if [ -f "$PERSIST_DB" ] && [ -s "$PERSIST_DB" ]; then
    _INIT_SIZE=$(wc -c < "$PERSIST_DB" 2>/dev/null | tr -d ' \t\n\r' || echo "0")
    echo "[PERSISTENCE] Found OmniRoute snapshot at: ${PERSIST_DB} (${_INIT_SIZE} bytes)"
    _RESTORED=0
    if command -v sqlite3 >/dev/null 2>&1; then
        _CHK=$(sqlite3 "$PERSIST_DB" "PRAGMA quick_check;" 2>/dev/null || echo "failed")
        if [ "$_CHK" = "ok" ]; then
            rm -f "$RUNTIME_DIR/storage.sqlite"* 2>/dev/null || true
            cp -f "$PERSIST_DB" "$RUNTIME_DB" 2>/dev/null || true
            echo "[PERSISTENCE] Restored persistent database into ${RUNTIME_DIR} successfully."
            _RESTORED=1
        elif [ -f "${BACKUP_DIR}/last-known-good.sqlite" ]; then
            _BCHK=$(sqlite3 "${BACKUP_DIR}/last-known-good.sqlite" "PRAGMA quick_check;" 2>/dev/null || echo "failed")
            if [ "$_BCHK" = "ok" ]; then
                rm -f "$RUNTIME_DIR/storage.sqlite"* 2>/dev/null || true
                cp -f "${BACKUP_DIR}/last-known-good.sqlite" "$RUNTIME_DB" 2>/dev/null || true
                cp -f "${BACKUP_DIR}/last-known-good.sqlite" "$PERSIST_DB" 2>/dev/null || true
                echo "[PERSISTENCE] Restored from last-known-good backup into ${RUNTIME_DB} successfully."
                _RESTORED=1
            fi
        fi
    fi
    if [ $_RESTORED -eq 0 ]; then
        rm -f "$RUNTIME_DIR/storage.sqlite"* 2>/dev/null || true
        cp -f "$PERSIST_DB" "$RUNTIME_DB" 2>/dev/null || true
        echo "[PERSISTENCE] Restored persistent database snapshot directly into ${RUNTIME_DB}."
    fi
fi

# Restore supplementary state directories
for _ITEM in oauth credentials runtime gemini_cli config_dir; do
    if [ -d "${PERSIST_DIR}/${_ITEM}" ]; then
        if [ "$_ITEM" = "gemini_cli" ]; then
            mkdir -p /root/.gemini 2>/dev/null || true
            cp -af "${PERSIST_DIR}/gemini_cli/"* /root/.gemini/ 2>/dev/null || true
        elif [ "$_ITEM" = "config_dir" ]; then
            mkdir -p /root/.config 2>/dev/null || true
            cp -af "${PERSIST_DIR}/config_dir/"* /root/.config/ 2>/dev/null || true
        else
            mkdir -p "${RUNTIME_DIR}/${_ITEM}" 2>/dev/null || true
            cp -af "${PERSIST_DIR}/${_ITEM}/"* "${RUNTIME_DIR}/${_ITEM}/" 2>/dev/null || true
        fi
    fi
done

# Database WAL Backup Synchronization Function
sync_omniroute_db() {
    if [ -f "$RUNTIME_DB" ] && [ -s "$RUNTIME_DB" ]; then
        mkdir -p "$PERSIST_DIR" "$BACKUP_DIR" 2>/dev/null || true
        _SYNC_TMP="${PERSIST_DB}.tmp"
        rm -f "$_SYNC_TMP" 2>/dev/null || true

        if command -v sqlite3 >/dev/null 2>&1; then
            sqlite3 "$RUNTIME_DB" "PRAGMA wal_checkpoint(TRUNCATE);" 2>/dev/null || true
            sqlite3 "$RUNTIME_DB" ".backup '$_SYNC_TMP'" 2>/dev/null || cp -f "$RUNTIME_DB" "$_SYNC_TMP" 2>/dev/null || true
        else
            cp -f "$RUNTIME_DB" "$_SYNC_TMP" 2>/dev/null || true
        fi

        _CHK="failed"
        if [ -f "$_SYNC_TMP" ] && [ -s "$_SYNC_TMP" ]; then
            if command -v sqlite3 >/dev/null 2>&1; then
                _CHK=$(sqlite3 "$_SYNC_TMP" "PRAGMA quick_check;" 2>/dev/null || echo "failed")
            else
                _CHK="ok"
            fi
        fi

        if [ "$_CHK" = "ok" ]; then
            mv -f "$_SYNC_TMP" "$PERSIST_DB" 2>/dev/null || true
            rm -f "${PERSIST_DB}-wal" "${PERSIST_DB}-shm" 2>/dev/null || true

            cp -f "$PERSIST_DB" "${BACKUP_DIR}/last-known-good.sqlite" 2>/dev/null || true
            TIMESTAMP=$(date +%Y%m%d-%H%M)
            cp -f "$PERSIST_DB" "${BACKUP_DIR}/storage-${TIMESTAMP}.sqlite" 2>/dev/null || true
            ls -t "${BACKUP_DIR}"/storage-*.sqlite 2>/dev/null | tail -n +6 | xargs rm -f 2>/dev/null || true

            for _ITEM in oauth credentials runtime; do
                if [ -d "${RUNTIME_DIR}/${_ITEM}" ]; then
                    mkdir -p "${PERSIST_DIR}/${_ITEM}" 2>/dev/null || true
                    cp -af "${RUNTIME_DIR}/${_ITEM}/"* "${PERSIST_DIR}/${_ITEM}/" 2>/dev/null || true
                fi
            done

            if [ -d "/root/.gemini" ]; then
                mkdir -p "${PERSIST_DIR}/gemini_cli" 2>/dev/null || true
                cp -af /root/.gemini/* "${PERSIST_DIR}/gemini_cli/" 2>/dev/null || true
            fi

            if [ -d "/root/.config" ]; then
                mkdir -p "${PERSIST_DIR}/config_dir" 2>/dev/null || true
                cp -af /root/.config/* "${PERSIST_DIR}/config_dir/" 2>/dev/null || true
            fi
            _SIZE=$(wc -c < "$PERSIST_DB" 2>/dev/null | tr -d ' \t\n\r' || echo "0")
            echo "[PERSISTENCE] Snapshot OK: ${_SIZE} bytes synced to ${PERSIST_DB}"
        else
            rm -f "$_SYNC_TMP" 2>/dev/null || true
            echo "[PERSISTENCE] WARNING: Database backup quick_check failed; skipping snapshot."
        fi
    fi
}

shutdown_gracefully() {
    echo "[SHUTDOWN] Process termination signal received. Flushing persistence state..."
    if [ -n "$OMNIROUTE_PID" ] && kill -0 $OMNIROUTE_PID 2>/dev/null; then
        kill -INT $OMNIROUTE_PID 2>/dev/null || true
    fi
    if [ -n "$HERMES_PID" ] && kill -0 $HERMES_PID 2>/dev/null; then
        kill -INT $HERMES_PID 2>/dev/null || true
    fi
    sleep 2
    sync_omniroute_db >/dev/null 2>&1 || true
    echo "[SHUTDOWN] Persistence sync finished cleanly. Container exiting."
    exit 0
}

trap shutdown_gracefully EXIT INT TERM

# ── STEP 3: Start FastAPI Gateway Immediately ────────────────────────────────
echo "[BOOT] FastAPI starting: $(get_elapsed)"
cd /
python3 -m uvicorn proxy:app --host 127.0.0.1 --port 8000 --workers 2 &
FASTAPI_PID=$!

# Step 4: Wait for FastAPI /health/live to return HTTP 200
FASTAPI_READY=0
for i in $(seq 1 30); do
    if curl --max-time 2 -sf -i http://127.0.0.1:8000/health/live | grep -q "200 OK"; then
        FASTAPI_READY=1
        END_TIME=$(date +%s)
        ELAPSED=$((END_TIME - START_TIME))
        echo "[BOOT] FastAPI live: ${ELAPSED}s"
        break
    fi
    sleep 0.2
done

if [ "$FASTAPI_READY" -ne 1 ]; then
    echo "[WARN] FastAPI did not report ready within timeout, proceeding with startup..."
fi

# Step 5: Start Nginx Edge Server on Public Port 4096
echo "[BOOT] Nginx starting..."
rm -f /etc/nginx/sites-enabled/default /etc/nginx/sites-available/default 2>/dev/null || true
nginx -t -c /nginx.conf
nginx -g 'daemon off;' -c /nginx.conf &
NGINX_PID=$!

# Step 6: Wait for Nginx /health/live on Port 4096 to return HTTP 200
NGINX_READY=0
for i in $(seq 1 30); do
    if curl --max-time 2 -sf -i http://127.0.0.1:4096/health/live | grep -q "200 OK"; then
        NGINX_READY=1
        break
    fi
    sleep 0.2
done

# Step 7: Signal Public Gateway Readiness immediately to Hugging Face
echo "[BOOT] Public gateway live"
echo "[BOOT] Background services starting asynchronously..."

    # Step 8: Start Redis Server & OmniRoute AI Gateway in Background
    if command -v redis-server >/dev/null 2>&1; then
        echo "[INIT] Starting Redis server on port 6379..."
        redis-server --daemonize yes 2>/dev/null || true
        if command -v redis-cli >/dev/null 2>&1; then
            redis-cli flushall 2>/dev/null || true
        fi
    fi

    echo "[INIT] Starting OmniRoute AI Gateway..."
    export PORT=20128
    export HOSTNAME="127.0.0.1"
    export DATA_DIR="/root/.omniroute"
    export REDIS_URL="redis://127.0.0.1:6379"
    export NEXT_PUBLIC_BASE_URL="https://jishnupg-opencode-cli.hf.space"
    export AUTH_COOKIE_SECURE="true"
    export ALLOW_REMOTE_OAUTH="true"
    export ALLOW_BROWSER_OAUTH="true"
    export OAUTH_CALLBACK_URL="https://jishnupg-opencode-cli.hf.space/callback"
    export NEXT_PUBLIC_OAUTH_CALLBACK_URL="https://jishnupg-opencode-cli.hf.space/callback"
    export REDIRECT_URI="https://jishnupg-opencode-cli.hf.space/callback"
    export ANTIGRAVITY_OAUTH_REDIRECT_URI="https://jishnupg-opencode-cli.hf.space/callback"
    export ANTIGRAVITY_REDIRECT_URI="https://jishnupg-opencode-cli.hf.space/callback"
    export ADMIN_PASSWORD="$INITIAL_PASSWORD"
    export OMNIROUTE_INITIAL_PASSWORD="$INITIAL_PASSWORD"
    export OMNIROUTE_PASSWORD="$INITIAL_PASSWORD"
    export PASSWORD="$INITIAL_PASSWORD"
    export RESET_PASSWORD="$INITIAL_PASSWORD"
    export OMNIROUTE_RESET_PASSWORD="$INITIAL_PASSWORD"
    export CLI_COMPAT_ANTIGRAVITY=1
    export CLI_COMPAT_GITHUB=1
    export CLI_COMPAT_KIMI_CODING=1
    export CLI_COMPAT_CLAUDE=1
    export CLI_COMPAT_CODEX=1
    export CLI_COMPAT_CURSOR=1
    export CLI_COMPAT_QWEN=1
    export OMNIROUTE_AUTO_FREE_FALLBACK_TO_FULL_POOL=true
    export OMNIROUTE_ALLOW_UNAUTHENTICATED=true
    export REQUIRE_API_KEY=false
    export OMNIROUTE_REQUIRE_API_KEY=false
    export OMNIROUTE_AUTH_REQUIRED=false
    export RATE_LIMIT_ENABLED=false
    export ENABLE_RATE_LIMIT=false
    export ENABLE_RATE_LIMITING=false
    export OMNIROUTE_RATE_LIMIT_ENABLED=false
    export AUTH_RATE_LIMIT=false
    export LOGIN_RATE_LIMIT=false
    export DISABLE_RATE_LIMIT=true
    export DISABLE_RATE_LIMITING=true
    export AUTH_RATE_LIMIT_MAX=100000
    export AUTH_RATE_LIMIT_WINDOW_MS=1000
    export RATE_LIMIT_MAX=100000
    export RATE_LIMIT_WINDOW_MS=1000
    export TRUST_PROXY=true

    # ── Master Provider API Keys & Secrets Auto-Discovery ─────────────────────
    export OPENAI_API_KEY="${OPENAI_API_KEY:-}"
    export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-${CLAUDE_API_KEY:-}}"
    export GEMINI_API_KEY="${GEMINI_API_KEY:-${GOOGLE_API_KEY:-${GOOGLE_GENAI_API_KEY:-}}}"
    export DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY:-}"
    export GROQ_API_KEY="${GROQ_API_KEY:-}"
    export NVIDIA_API_KEY="${NVIDIA_API_KEY:-${NVIDIA_NIM_API_KEY:-}}"
    export HUGGINGFACE_API_KEY="${HUGGINGFACE_API_KEY:-${HF_TOKEN:-${HUGGING_FACE_HUB_TOKEN:-}}}"
    export HF_TOKEN="${HF_TOKEN:-$HUGGINGFACE_API_KEY}"
    export OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-}"
    export MISTRAL_API_KEY="${MISTRAL_API_KEY:-}"
    export COHERE_API_KEY="${COHERE_API_KEY:-}"
    export MOONSHOT_API_KEY="${MOONSHOT_API_KEY:-${KIMI_API_KEY:-}}"
    export TOGETHER_API_KEY="${TOGETHER_API_KEY:-}"
    export FIREWORKS_API_KEY="${FIREWORKS_API_KEY:-}"
    export CEREBRAS_API_KEY="${CEREBRAS_API_KEY:-}"
    export PERPLEXITY_API_KEY="${PERPLEXITY_API_KEY:-}"
    export XAI_API_KEY="${XAI_API_KEY:-}"
    export GITHUB_TOKEN="${GITHUB_TOKEN:-${GH_TOKEN:-}}"
    export ANTIGRAVITY_API_KEY="${ANTIGRAVITY_API_KEY:-${ANTIGRAVITY_TOKEN:-}}"

    # ── Restore Persistent Credential Vault into Environment ──────────────────
    if [ -f "/data/omniroute/credentials_vault.json" ]; then
        echo "[VAULT] Restoring persistent provider secrets from /data/omniroute/credentials_vault.json..."
        python3 -c "
import json
try:
    with open('/data/omniroute/credentials_vault.json', 'r', encoding='utf-8') as f:
        v = json.load(f)
    for k, val in v.items():
        if k and val:
            print(f'export {k}=\"{val}\"')
except Exception:
    pass
" > /tmp/vault_env.sh 2>/dev/null || true
        if [ -f "/tmp/vault_env.sh" ]; then
            . /tmp/vault_env.sh
            rm -f /tmp/vault_env.sh
        fi
    fi

    if [ -d "/omniroute" ]; then
        cd /omniroute
        if [ -f "/fix_omniroute.py" ]; then
            python3 /fix_omniroute.py /omniroute 2>&1 || true
        elif [ -f "fix_omniroute.py" ]; then
            python3 fix_omniroute.py ./ 2>&1 || true
        fi
        mkdir -p /data/omniroute 2>/dev/null || true
        if [ -f "server.js" ]; then
            echo "[INIT] OmniRoute entry: node server.js (PORT=20128)"
            node server.js > /data/omniroute/omniroute.log 2>&1 &
        else
            echo "[INIT] OmniRoute entry: npm run start (PORT=20128)"
            npm run start > /data/omniroute/omniroute.log 2>&1 &
        fi
        OMNIROUTE_PID=$!
        (
            for i in $(seq 1 90); do
                if curl -fsS "http://127.0.0.1:20128/api/monitoring/health" >/dev/null 2>&1; then
                    echo "[HEALTH] OmniRoute ready after ${i}s"
                    echo "[PROCESS] OmniRoute: PID ${OMNIROUTE_PID}"
                    sync_omniroute_db
                    break
                fi
                sleep 1
            done
        ) &
    fi

# Step 9: Start Telegram Range Stream Proxy in Background (Port 8080)
# Optional: disabled by default to keep cpu-basic RAM/CPU for the chat path.
if [ "${ENABLE_TG_STREAMER:-0}" = "1" ] && [ -f "/tg_streamer.py" ]; then
    echo "[HEALTH] Telegram streamer starting in background..."
    python3 /tg_streamer.py > /data/cache/tg_streamer.log 2>&1 &
    TG_PID=$!
else
    echo "[INIT] Telegram streamer disabled (ENABLE_TG_STREAMER!=1)"
fi

if [ -f "/health_doctor.py" ]; then
    echo "[HEALTH] Starting Database & Disk Health Doctor daemon..."
    python3 /health_doctor.py > /data/cache/health_doctor.log 2>&1 &
fi

# Step 10: Start Jellyfin Media Server in Background (Port 8096)
# Optional: disabled by default — Jellyfin media scans starve OmniRoute on
# cpu-basic and cause multi-minute "Service initializing" windows.
WEBDIR_OPT=""
if [ -d "/usr/share/jellyfin/web" ]; then
    WEBDIR_OPT="--webdir /usr/share/jellyfin/web"
fi

if [ "${ENABLE_JELLYFIN:-0}" = "1" ]; then
    if command -v jellyfin >/dev/null 2>&1; then
        echo "[HEALTH] Jellyfin starting in background..."
        jellyfin --datadir /data/jellyfin/data --configdir /data/jellyfin/config --cachedir /data/jellyfin/cache --logdir /data/jellyfin/log $WEBDIR_OPT > /data/jellyfin/log/jellyfin.log 2>&1 &
        JELLYFIN_PID=$!
    elif [ -f "/usr/bin/jellyfin" ]; then
        echo "[HEALTH] Jellyfin binary starting in background..."
        /usr/bin/jellyfin --datadir /data/jellyfin/data --configdir /data/jellyfin/config --cachedir /data/jellyfin/cache --logdir /data/jellyfin/log $WEBDIR_OPT > /data/jellyfin/log/jellyfin.log 2>&1 &
        JELLYFIN_PID=$!
    fi
else
    echo "[INIT] Jellyfin disabled (ENABLE_JELLYFIN!=1)"
fi

# Step 11: Start Hermes Agent in Background (Port 8642)
if true; then
    echo "[HEALTH] Hermes Agent starting in background on port 8642..."
    mkdir -p /data/hermes/memories /data/hermes/skills /data/hermes/sessions 2>/dev/null || true
    chmod -R 777 /data/hermes 2>/dev/null || true
    if [ -d "/data/hermes" ] && [ "$(ls -A /data/hermes 2>/dev/null)" ]; then
        rsync -a /data/hermes/. /root/.hermes/ 2>/dev/null || \
            cp -rf /data/hermes/. /root/.hermes/ 2>/dev/null || true
        echo "[PERSISTENCE] Restored Hermes memory from /data/hermes"
    fi

    # Create global Python sitecustomize.py to enforce OmniRoute API base for all LLM calls
    for _SITEDIR in "/usr/local/lib/python3.11/dist-packages" "/usr/lib/python3.11" "/root/.hermes"; do
        if [ -d "$_SITEDIR" ]; then
            cat > "${_SITEDIR}/sitecustomize.py" << 'PYCUSTOM'
import os
os.environ["OPENAI_API_BASE"] = "http://127.0.0.1:20128/v1"
os.environ["OPENAI_API_BASE_URL"] = "http://127.0.0.1:20128/v1"
os.environ["OPENAI_BASE_URL"] = "http://127.0.0.1:20128/v1"
os.environ["OPENAI_API_KEY"] = os.getenv("OMNIROUTE_API_KEY", "sk-6646a5f2024f6318-d27ff7-f3e152c8")
os.environ["HERMES_API_BASE_URL"] = "http://127.0.0.1:20128/v1"
os.environ["HERMES_API_KEY"] = os.getenv("OMNIROUTE_API_KEY", "sk-6646a5f2024f6318-d27ff7-f3e152c8")
PYCUSTOM
        fi
    done
    export PYTHONPATH="/:/root/.hermes:/usr/local/lib/python3.11/dist-packages:${PYTHONPATH}"

    if [ -n "${HERMES_API_KEY}" ] && [ "${HERMES_API_KEY}" != "sk-6646a5f2024f6318-d27ff7-f3e152c8" ] && [ "${HERMES_API_KEY}" != "admin123" ]; then
        export HF_HERMES_API_KEY="${HERMES_API_KEY}"
    fi

    HERMES_LLM_KEY="${OMNIROUTE_API_KEY:-sk-6646a5f2024f6318-d27ff7-f3e152c8}"
    export HERMES_API_BASE_URL="http://127.0.0.1:8000/v1"
    export HERMES_API_KEY="${HERMES_LLM_KEY}"
    export OPENAI_API_BASE="http://127.0.0.1:8000/v1"
    export OPENAI_API_KEY="${HERMES_LLM_KEY}"
    export HERMES_MODEL="${HERMES_MODEL:-antigravity/gemini-3.6-flash-medium}"
    export HERMES_DATA_DIR="/root/.hermes"
    export HERMES_GATEWAY_PORT=8642
    export HERMES_PORT=8642
    export PORT=8642
    export API_SERVER_ENABLED=true
    export API_SERVER_PORT=8642
    export API_SERVER_HOST=127.0.0.1
    export API_SERVER_KEY="${HF_HERMES_API_KEY:-${HERMES_GATEWAY_API_KEY:-${HERMES_API_KEY_SECRET:-${API_KEY_SECRET:-${INITIAL_PASSWORD:-sk-6646a5f2024f6318-d27ff7-f3e152c8}}}}}"
    export HERMES_GATEWAY_API_KEY="${API_SERVER_KEY}"
    export HERMES_GATEWAY_ENABLED=true

    cat > /root/.hermes/.env << HERMES_ENV
API_SERVER_ENABLED=true
API_SERVER_PORT=8642
API_SERVER_HOST=127.0.0.1
API_SERVER_KEY=${API_SERVER_KEY}
OPENAI_API_BASE=http://127.0.0.1:8000/v1
OPENAI_API_BASE_URL=http://127.0.0.1:8000/v1
OPENAI_API_KEY=${HERMES_LLM_KEY}
HERMES_API_BASE_URL=http://127.0.0.1:8000/v1
HERMES_API_KEY=${HERMES_LLM_KEY}
DEFAULT_MODEL=${HERMES_MODEL}
TELEGRAM_ENABLED=${TELEGRAM_BOT_TOKEN:+true}
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
TELEGRAM_ALLOW_ALL_USERS=true
TELEGRAM_ALLOWED_USERS=${TELEGRAM_ALLOWED_USERS:-*}
HERMES_ENV

    cat > /root/.hermes/config.json << HERMES_CFG
{
  "api_base_url": "http://127.0.0.1:8000/v1",
  "api_key": "${HERMES_LLM_KEY}",
  "model": "${HERMES_MODEL}",
  "data_dir": "/root/.hermes",
  "api_server": {
    "enabled": true,
    "port": 8642,
    "host": "127.0.0.1",
    "key": "${API_SERVER_KEY}"
  },
  "gateway": {
    "enabled": true,
    "port": 8642,
    "api_key": "${API_SERVER_KEY}"
  },
  "memory": {
    "enabled": true,
    "sqlite_fts5": true,
    "memory_file": "/root/.hermes/memories/MEMORY.md",
    "user_file": "/root/.hermes/memories/USER.md"
  },
  "tools": {
    "web_search": true,
    "web_extract": true,
    "browser_automation": true
  },
  "stt": {
    "enabled": false
  },
  "tts": {
    "enabled": false
  }
}
HERMES_CFG

    mkdir -p /data/hermes 2>/dev/null || true
    cp -f /root/.hermes/.env /data/hermes/.env 2>/dev/null || true
    cp -f /root/.hermes/config.json /data/hermes/config.json 2>/dev/null || true

    if [ -n "$TELEGRAM_BOT_TOKEN" ]; then
        export HERMES_TELEGRAM_TOKEN="$TELEGRAM_BOT_TOKEN"
        export HERMES_TELEGRAM_ENABLED=true
        export TELEGRAM_ALLOW_ALL_USERS=true
        export TELEGRAM_ALLOWED_USERS="${TELEGRAM_ALLOWED_USERS:-*}"
        echo "[HERMES] Telegram bot integration enabled"
    fi

    HERMES_START_CMD=""
    if python3 -c "import hermes; print(hermes)" >/dev/null 2>&1 && hermes gateway --help >/dev/null 2>&1; then
        HERMES_START_CMD="hermes gateway"
    else
        HERMES_START_CMD="python3 -m uvicorn gateway.hermes_standalone:app --host 127.0.0.1 --port 8642"
    fi

    if [ -n "$HERMES_START_CMD" ]; then
        $HERMES_START_CMD 2>&1 | tee /data/cache/hermes.log | sed 's/^/[HERMES] /' &
        HERMES_PID=$!
        echo "[PROCESS] Hermes Agent: PID ${HERMES_PID}"
        for i in $(seq 1 30); do
            if ! kill -0 $HERMES_PID 2>/dev/null; then
                echo "[HERMES] ERROR: Process died immediately."
                HERMES_PID=""
                break
            fi
            if curl -fsS "http://127.0.0.1:8642/health" >/dev/null 2>&1 || \
               curl -fsS "http://127.0.0.1:8642/v1/models" >/dev/null 2>&1; then
                echo "[HEALTH] Hermes Agent ready after ${i}s"
                break
            fi
            sleep 1
        done
    fi
fi

# Step 12: Keep PID 1 Alive and Monitor Child Processes
echo "[BOOT] All services dispatched. Process Supervisor active."

while true; do
    if [ -n "$FASTAPI_PID" ] && ! kill -0 $FASTAPI_PID 2>/dev/null; then
        echo "[CRITICAL] FastAPI Gateway process died! Restarting..."
        python3 -m uvicorn proxy:app --host 127.0.0.1 --port 8000 --workers 2 > /data/cache/fastapi_gateway.log 2>&1 &
        FASTAPI_PID=$!
    fi

    if [ -n "$NGINX_PID" ] && ! kill -0 $NGINX_PID 2>/dev/null; then
        echo "[CRITICAL] Nginx process died! Restarting..."
        nginx -g 'daemon off;' -c /nginx.conf &
        NGINX_PID=$!
    fi

    if [ -n "$OMNIROUTE_PID" ] && ! kill -0 $OMNIROUTE_PID 2>/dev/null; then
        echo "[CRITICAL] OmniRoute process died! Restarting..."
        (cd /omniroute && node server.js) > /data/omniroute/omniroute.log 2>&1 &
        OMNIROUTE_PID=$!
    fi

    if [ -n "$HERMES_PID" ] && [ -n "$HERMES_START_CMD" ] && ! kill -0 $HERMES_PID 2>/dev/null; then
        echo "[CRITICAL] Hermes Agent process died! Restarting..."
        $HERMES_START_CMD 2>&1 | tee /data/cache/hermes.log | sed 's/^/[HERMES] /' &
        HERMES_PID=$!
    fi

    # Live background sync of database & credentials vault every 10 seconds
    sync_omniroute_db >/dev/null 2>&1 || true

    sleep 10
done
