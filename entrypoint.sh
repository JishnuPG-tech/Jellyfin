#!/bin/sh
# OpenCode-Serve entrypoint
#
#  nginx  :7860  (HF exposed)
#    /terminal  → ttyd  :7681  — real PTY bash, mobile-optimised
#    /          → opencode :8080 — chat UI + REST API + SSE
#  sshd   :22   (Cloudflare Named Tunnel → Termius)
#
set -u

echo "============================================"
echo "=== OpenCode-Serve starting               ==="
echo "Time: $(date)"
echo "============================================"

# Prevent git ownership errors
git config --global --add safe.directory '*' 2>/dev/null || true

# ─── Data directories ───
# /data is no longer a mounted volume — sync_engine restores its contents
# from the HF Dataset repo below. We create the dirs here so every
# subsequent step has a guaranteed path to write to.
echo "[INIT] Setting up /data directories..."
mkdir -p /data/share/opencode 2>/dev/null || echo "[WARN] Could not create /data/share/opencode"
mkdir -p /data/config/opencode 2>/dev/null || echo "[WARN] Could not create /data/config/opencode"
mkdir -p /data/cache/opencode 2>/dev/null || echo "[WARN] Could not create /data/cache/opencode"
mkdir -p /data/state/opencode 2>/dev/null || echo "[WARN] Could not create /data/state/opencode"
mkdir -p /data/workspaces /data/logs 2>/dev/null || true

# ─── Restore persistent storage from HF Dataset ──────────────────────
# This pulls /projects/default (workspace), /data/share/opencode (OpenCode DB),
# and /data/config/opencode (OpenCode config) from the dataset repo so the user
# continues exactly where they left off after a container rebuild.
echo "[RESTORE] Restoring workspace from HF Dataset..."
python3 /sync_engine.py restore 2>&1 | tee -a /data/logs/sync.log
echo "[RESTORE] Done."

# ─── OpenCode config ─────────────────────────────────────────────────
# Remove stale model configs that use wrong format
echo "[CONFIG] Setting up default configuration..."
python3 -c "
import json, os
p = '/data/config/opencode/opencode.json'
stale_models = ['big-pickle', 'mimo-v2.5-free', 'opencode/mimo-v2.5-free']
try:
    d = json.load(open(p)) if os.path.exists(p) else {}
    if d.get('model') in stale_models:
        print(f'[CONFIG] Removing stale model {d[\"model\"]!r}, will regenerate')
        del d['model']
        json.dump(d, open(p, 'w'), indent=2)
except Exception as e:
    print(f'[CONFIG] Error normalizing: {e}')
" 2>/dev/null || true

# Always write the server block so OpenCode binds on port 8080 (nginx proxies to it)
# and the model is set correctly. The server is behind nginx so hostname is 127.0.0.1.
python3 -c "
import json, os
p = '/data/config/opencode/opencode.json'
try:
    d = json.load(open(p)) if os.path.exists(p) else {}
except Exception:
    d = {}
d['\$schema'] = 'https://opencode.ai/config.json'
# Always overwrite port/hostname — old containers may have stored port 4096
d['server'] = {'port': 8080, 'hostname': '127.0.0.1'}
if not os.environ.get('ANTHROPIC_API_KEY') and not os.environ.get('OPENAI_API_KEY'):
    d['model'] = 'opencode/big-pickle'
elif not d.get('model'):
    d['model'] = 'opencode/big-pickle'
json.dump(d, open(p, 'w'), indent=2)
print('[CONFIG] Wrote config with model:', d.get('model'))
" 2>/dev/null || true
echo "[CONFIG] Current configuration:"
cat /data/config/opencode/opencode.json 2>/dev/null || echo "{}"

# ─── Detect and remove malformed SQLite databases ───
echo "[DB] Checking database integrity..."
DB_PATH="/data/share/opencode/opencode.db"
if [ -f "$DB_PATH" ]; then
    echo "[DB] Found database at $DB_PATH"
    python3 -c "
import sqlite3
try:
    conn = sqlite3.connect('$DB_PATH', timeout=5)
    row = conn.execute('PRAGMA integrity_check').fetchone()
    conn.close()
    print('[DB] Integrity:', row[0] if row else 'error')
except Exception as e:
    print('[DB] Error:', e)
" 2>/dev/null || echo "[DB] Could not read database"
else
    echo "[DB] No database found (fresh start)"
fi

# ─── nginx: minimal reverse proxy ───────────────────────────────────
cat > /etc/nginx/nginx.conf << 'NGINX_CONF'
events { worker_connections 1024; }
http {
    include       /etc/nginx/mime.types;
    default_type  application/octet-stream;

    server {
        listen 7860;

        # Terminal (ttyd PTY)
        location /terminal {
            proxy_pass         http://127.0.0.1:7681;
            proxy_http_version 1.1;
            proxy_set_header   Upgrade $http_upgrade;
            proxy_set_header   Connection "upgrade";
            proxy_set_header   Host $host;
            proxy_read_timeout 86400;
        }

        # OpenCode — everything else
        location / {
            proxy_pass         http://127.0.0.1:8080;
            proxy_http_version 1.1;
            proxy_set_header   Upgrade $http_upgrade;
            proxy_set_header   Connection $http_connection;
            proxy_set_header   Host $host;
            proxy_set_header   X-Real-IP $remote_addr;
            proxy_buffering    off;
            proxy_read_timeout 86400;
        }
    }
}
NGINX_CONF
nginx
echo "[NGINX] Started."

# ─── SSH server ──────────────────────────────────────────────────────
# Listens on port 22 (internal only — Cloudflare Named Tunnel exposes it).
# Password is set via SSH_PASSWORD Space Secret.
# Falls back to a generated password logged to Space logs if not set.
echo "[SSH] Configuring SSH server..."

# Harden sshd config
cat > /etc/ssh/sshd_config << 'SSHD_CONF'
Port 22
PermitRootLogin yes
PasswordAuthentication yes
PubkeyAuthentication yes
AuthorizedKeysFile .ssh/authorized_keys
ChallengeResponseAuthentication no
UsePAM no
X11Forwarding no
PrintMotd no
AcceptEnv LANG LC_*
Subsystem sftp /usr/lib/openssh/sftp-server
SSHD_CONF

# Set SSH password — use SSH_PASSWORD secret if provided, else generate one
if [ -n "${SSH_PASSWORD:-}" ]; then
    echo "root:${SSH_PASSWORD}" | chpasswd
    echo "[SSH] Password set from SSH_PASSWORD secret."
else
    GENERATED_PW=$(head -c 16 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 16)
    echo "root:${GENERATED_PW}" | chpasswd
    echo "[SSH] ============================================"
    echo "[SSH] No SSH_PASSWORD secret set."
    echo "[SSH] Generated password: ${GENERATED_PW}"
    echo "[SSH] Set SSH_PASSWORD in Space Secrets to make it permanent."
    echo "[SSH] ============================================"
fi

# Generate host keys if missing (fresh container)
ssh-keygen -A 2>/dev/null || true

# Start sshd
/usr/sbin/sshd
echo "[SSH] sshd started on port 22."

# ─── Verify sshd is actually listening on port 22 ────────────────────
# Uses ss(8) from iproute2. Fails hard if sshd did not bind — a tunnel
# to a port nobody is listening on is worse than no tunnel at all.
echo "[SSH] Verifying sshd is listening on port 22..."
SSHD_LISTEN=""
for _i in $(seq 1 10); do
    SSHD_LISTEN=$(ss -tlnp 2>/dev/null | grep ' 0\.0\.0\.0:22 \|:22 ' || true)
    [ -n "$SSHD_LISTEN" ] && break
    sleep 1
done

if [ -n "$SSHD_LISTEN" ]; then
    echo "[SSH] Confirmed: sshd is listening on port 22."
    echo "[SSH] $SSHD_LISTEN"
else
    echo "[SSH] ============================================"
    echo "[SSH] FATAL: sshd is NOT listening on port 22."
    echo "[SSH] Active TCP listeners:"
    ss -tlnp 2>/dev/null || true
    echo "[SSH] ============================================"
    echo "[SSH] Cannot start Cloudflare tunnel — no SSH service to tunnel to."
    # Do not exit — OpenCode and the terminal should still work.
    # The operator needs to investigate why sshd failed to bind.
fi

# ─── Cloudflare Named Tunnel ──────────────────────────────────────────
# Requires CF_TUNNEL_TOKEN set in Space Secrets.
# The token is obtained from the Cloudflare Zero Trust dashboard:
#   Zero Trust → Networks → Tunnels → select/create tunnel → Overview tab → copy token
#
# The named tunnel must be configured in Cloudflare Zero Trust to route
# the public hostname to ssh://localhost:22 (service: SSH, URL: localhost:22).
#
# NOTE: The token is NEVER printed or logged.
echo "[TUNNEL] ============================================"
CF_LOG=/data/logs/cloudflared.log

if [ -z "${CF_TUNNEL_TOKEN:-}" ]; then
    echo "[TUNNEL] WARNING: CF_TUNNEL_TOKEN secret is not set."
    echo "[TUNNEL] SSH access via Cloudflare Named Tunnel is DISABLED."
    echo "[TUNNEL] To enable:"
    echo "[TUNNEL]   1. Create a Named Tunnel in Cloudflare Zero Trust"
    echo "[TUNNEL]      (Zero Trust → Networks → Tunnels → Add a tunnel)"
    echo "[TUNNEL]   2. Configure the public hostname to route to ssh://localhost:22"
    echo "[TUNNEL]   3. Copy the tunnel token from the Cloudflare dashboard"
    echo "[TUNNEL]   4. Add CF_TUNNEL_TOKEN to Space Secrets"
    echo "[TUNNEL] OpenCode and terminal will start normally without it."
    echo "[TUNNEL] ============================================"
else
    echo "[TUNNEL] Named tunnel starting..."
    # Use 'run --token' — the only correct form for Named Tunnels.
    # Do NOT use 'tunnel --url' (Quick Tunnel) — it creates ephemeral
    # trycloudflare.com addresses that SSH clients cannot reliably use.
    cloudflared tunnel run \
        --token "${CF_TUNNEL_TOKEN}" \
        --no-autoupdate \
        --logfile "${CF_LOG}" \
        --loglevel info \
        > /dev/null 2>&1 &
    CF_PID=$!

    # ── Wait up to 40 s for the tunnel to register ───────────────────
    TUNNEL_CONNECTED=0
    for _i in $(seq 1 40); do
        sleep 1
        # cloudflared Named Tunnel logs "Registered tunnel connection" on success
        if grep -qi \
            "Registered tunnel connection\|registered tunnel connection\|Connection registered" \
            "${CF_LOG}" 2>/dev/null; then
            TUNNEL_CONNECTED=1
            break
        fi
        # Detect fatal auth errors early — no point waiting the full 40 s
        if grep -qi \
            "Invalid.*token\|token.*invalid\|failed to authenticate\|authentication failed\|ERR_FAILED_TO_AUTHENTICATE\|Invalid tunnel credentials\|failed to validate token" \
            "${CF_LOG}" 2>/dev/null; then
            echo "[TUNNEL] ERROR: Cloudflare authentication failed — aborting wait."
            break
        fi
    done

    if [ "$TUNNEL_CONNECTED" = "1" ]; then
        # ── Extract metadata from JSON log lines ──────────────────────
        # cloudflared Named Tunnel writes structured JSON; field values
        # are extracted with portable sed without exposing the token.
        CONNECTOR_ID=$(grep -o '"connectorID":"[^"]*"' "${CF_LOG}" 2>/dev/null \
            | head -1 | sed 's/"connectorID":"//;s/"//')
        TUNNEL_NAME=$(grep -o '"tunnelName":"[^"]*"' "${CF_LOG}" 2>/dev/null \
            | head -1 | sed 's/"tunnelName":"//;s/"//')
        TUNNEL_ID=$(grep -o '"tunnelID":"[^"]*"' "${CF_LOG}" 2>/dev/null \
            | head -1 | sed 's/"tunnelID":"//;s/"//')
        CONN_LOCATION=$(grep -o '"location":"[^"]*"' "${CF_LOG}" 2>/dev/null \
            | head -1 | sed 's/"location":"//;s/"//')
        CONN_PROTOCOL=$(grep -o '"protocol":"[^"]*"' "${CF_LOG}" 2>/dev/null \
            | head -1 | sed 's/"protocol":"//;s/"//')

        echo "[TUNNEL] Named tunnel connected"
        echo "[TUNNEL] ============================================"
        [ -n "$CONNECTOR_ID"   ] && echo "[TUNNEL] Connector ID  : ${CONNECTOR_ID}"
        [ -n "$TUNNEL_NAME"    ] && echo "[TUNNEL] Tunnel name   : ${TUNNEL_NAME}"
        [ -n "$TUNNEL_ID"      ] && echo "[TUNNEL] Tunnel ID     : ${TUNNEL_ID}"
        [ -n "$CONN_LOCATION"  ] && echo "[TUNNEL] Edge location : ${CONN_LOCATION}"
        [ -n "$CONN_PROTOCOL"  ] && echo "[TUNNEL] Protocol      : ${CONN_PROTOCOL}"
        echo "[TUNNEL] SSH port      : 22"
        echo "[TUNNEL] SSH user      : root"
        echo "[TUNNEL] Connect via   : the public hostname configured in"
        echo "[TUNNEL]                 Cloudflare Zero Trust for this tunnel."
        echo "[TUNNEL] Logs          : ${CF_LOG}"
        echo "[TUNNEL] ============================================"
    else
        # ── Diagnose the failure ──────────────────────────────────────
        echo "[TUNNEL] ============================================"
        echo "[TUNNEL] WARNING: Named tunnel did NOT confirm connection within 40 s."
        echo "[TUNNEL] Connection status: FAILED"
        echo "[TUNNEL] ----"

        # Show recent log lines — strip any line containing the token pattern
        # (tokens are long base64 strings; we never echo the CF_TUNNEL_TOKEN var).
        echo "[TUNNEL] Recent cloudflared output:"
        tail -25 "${CF_LOG}" 2>/dev/null \
            | grep -v -i "token\|credential\|secret\|password" \
            || echo "[TUNNEL] (log not yet written)"
        echo "[TUNNEL] ----"

        # Specific diagnoses
        if grep -qi \
            "Invalid.*token\|token.*invalid\|failed to validate token\|Invalid tunnel credentials\|ERR_FAILED_TO_AUTHENTICATE\|authentication failed\|failed to authenticate" \
            "${CF_LOG}" 2>/dev/null; then
            echo "[TUNNEL] DIAGNOSIS : Invalid or expired tunnel token."
            echo "[TUNNEL] ACTION    : Regenerate the token in Cloudflare Zero Trust:"
            echo "[TUNNEL]             Zero Trust → Networks → Tunnels → select tunnel"
            echo "[TUNNEL]             → Overview tab → copy new token"
            echo "[TUNNEL]             Update CF_TUNNEL_TOKEN in Space Secrets."

        elif grep -qi "reconnect\|retrying connection\|retry connection" \
            "${CF_LOG}" 2>/dev/null; then
            echo "[TUNNEL] DIAGNOSIS : Tunnel is in a reconnect loop."
            echo "[TUNNEL] ACTION    : Check Cloudflare service status and Space outbound"
            echo "[TUNNEL]             network connectivity."

        elif grep -qi \
            "dial tcp\|connection refused\|no such host\|network unreachable\|i/o timeout\|TLS handshake" \
            "${CF_LOG}" 2>/dev/null; then
            echo "[TUNNEL] DIAGNOSIS : Network error — cannot reach the Cloudflare edge."
            echo "[TUNNEL] ACTION    : Verify outbound HTTPS/QUIC is not blocked from the Space."

        elif grep -qi "already running\|tunnel already" \
            "${CF_LOG}" 2>/dev/null; then
            echo "[TUNNEL] DIAGNOSIS : A duplicate tunnel connector may already be running."
            echo "[TUNNEL] ACTION    : Check your Cloudflare Zero Trust Tunnels list for"
            echo "[TUNNEL]             stale connectors and remove them."

        else
            echo "[TUNNEL] DIAGNOSIS : Unknown — inspect full log for details."
            echo "[TUNNEL] Log path  : ${CF_LOG}"
        fi

        echo "[TUNNEL] ============================================"
        echo "[TUNNEL] OpenCode and terminal continue normally."
        echo "[TUNNEL] SSH access via Cloudflare will be unavailable until resolved."
    fi
fi

# ─── Start DB self-healing daemon ────────────────────────────────────
echo "[CLEANER] Starting self-healing daemon..."
python3 /cleaner.py &

# ─── Start background sync daemon ────────────────────────────────────
# Runs every 15 s, commits only changed files to the HF Dataset.
# The AI and terminal always work on the local filesystem — sync is
# purely a background backup layer and never blocks any operation.
echo "[SYNC] Starting background sync daemon..."
python3 /sync_engine.py watch 2>&1 | tee -a /data/logs/sync.log &
echo "[SYNC] Sync daemon started. Logs: /data/logs/sync.log"

# ─── Ensure project dir exists ───────────────────────────────────────
mkdir -p /projects/default
cd /projects/default
[ -d .git ] || git init -q 2>/dev/null || true

# ─── ttyd: real PTY bash on :7681 ────────────────────────────────────
echo "[TERMINAL] ttyd on :7681 (base-path /terminal) ..."
nohup ttyd -p 7681 -i 0.0.0.0 \
  -b /terminal \
  -W \
  -t fontSize=15 \
  -t lineHeight=1.1 \
  -t cursorBlink=true \
  -t scrollback=2000 \
  bash -l > /data/logs/ttyd.log 2>&1 &

# ─── Test OpenCode Zen API reachability ─────────────────────────────
echo "[NET] Testing OpenCode Zen API..."
ZEN_STATUS=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 "https://opencode.ai/zen/v1/models" 2>/dev/null || echo "000")
echo "[NET] OpenCode Zen API: $ZEN_STATUS"
if [ "$ZEN_STATUS" != "200" ]; then
    echo "[NET] WARNING: Zen API unreachable — free model responses may fail"
fi

# ─── OpenCode on :8080 (nginx proxies / → here) ──────────────────────
echo "[OPENCODE] opencode serve on :8080 ..."
exec opencode serve --port 8080 --hostname 127.0.0.1
