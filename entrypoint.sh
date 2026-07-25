#!/bin/sh
# OpenCode-Serve entrypoint
#
#  nginx    :7860  (HF exposed)
#    /terminal  → ttyd  :7681  — real PTY bash, mobile-optimised
#    /          → opencode :8080 — chat UI + REST API + SSE
#  sshd     :22   (internal only)
#    ↑ reached via reverse SSH tunnel → Fly.io jump server → Termius
#
set -u

echo "============================================"
echo "=== OpenCode-Serve starting               ==="
echo "Time: $(date)"
echo "============================================"

# Prevent git ownership errors
git config --global --add safe.directory '*' 2>/dev/null || true

# ─── Data directories ───
echo "[INIT] Setting up /data directories..."
mkdir -p /data/share/opencode 2>/dev/null || echo "[WARN] Could not create /data/share/opencode"
mkdir -p /data/config/opencode 2>/dev/null || echo "[WARN] Could not create /data/config/opencode"
mkdir -p /data/cache/opencode 2>/dev/null || echo "[WARN] Could not create /data/cache/opencode"
mkdir -p /data/state/opencode 2>/dev/null || echo "[WARN] Could not create /data/state/opencode"
mkdir -p /data/workspaces /data/logs /root/.ssh 2>/dev/null || true
chmod 700 /root/.ssh

# ─── Restore persistent storage from HF Dataset ──────────────────────
echo "[RESTORE] Restoring workspace from HF Dataset..."
python3 /sync_engine.py restore 2>&1 | tee -a /data/logs/sync.log
echo "[RESTORE] Done."

# ─── OpenCode config ─────────────────────────────────────────────────
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

python3 -c "
import json, os
p = '/data/config/opencode/opencode.json'
try:
    d = json.load(open(p)) if os.path.exists(p) else {}
except Exception:
    d = {}
d['\$schema'] = 'https://opencode.ai/config.json'
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
# sshd listens on port 22 internally.
# Port 22 is NOT exposed by HF — it is reached via the reverse SSH
# tunnel that this container opens to the Fly.io jump server.
# Termius → Fly.io:2222 → (tunnel) → here:22
echo "[SSH] Configuring sshd..."

cat > /etc/ssh/sshd_config << 'SSHD_CONF'
Port 22
PermitRootLogin yes
PasswordAuthentication yes
PubkeyAuthentication yes
AuthorizedKeysFile /root/.ssh/authorized_keys
ChallengeResponseAuthentication no
UsePAM no
X11Forwarding no
PrintMotd no
AcceptEnv LANG LC_*
Subsystem sftp /usr/lib/openssh/sftp-server
SSHD_CONF

# Set SSH password for Termius login
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

ssh-keygen -A 2>/dev/null || true
/usr/sbin/sshd
echo "[SSH] sshd running on port 22 (internal)."

# ─── Reverse SSH tunnel → Fly.io jump server ─────────────────────────
# Required Space Secrets:
#   JUMP_HOST       — Fly.io hostname, e.g. opencode-jump.fly.dev
#   JUMP_SSH_KEY    — private ed25519 key (matching the public key stored
#                     in JUMP_AUTHORIZED_KEY on the Fly.io side)
#
# What this creates:
#   Fly.io port 2222 → this container's localhost:22
#   Termius connects to <JUMP_HOST>:2222 with SSH_PASSWORD
#
# HF does NOT flag outbound SSH — only inbound tunnel services are blocked.
echo "[TUNNEL] ============================================"

if [ -z "${JUMP_HOST:-}" ] || [ -z "${JUMP_SSH_KEY:-}" ]; then
    echo "[TUNNEL] Reverse SSH tunnel DISABLED."
    [ -z "${JUMP_HOST:-}"    ] && echo "[TUNNEL] Missing secret: JUMP_HOST"
    [ -z "${JUMP_SSH_KEY:-}" ] && echo "[TUNNEL] Missing secret: JUMP_SSH_KEY"
    echo "[TUNNEL] See setup instructions — tunnel will not start."
    echo "[TUNNEL] Terminal at /terminal still works without it."
else
    # Write private key to disk (never logged)
    JUMP_KEY_FILE=/root/.ssh/jump_key
    printf '%s\n' "${JUMP_SSH_KEY}" > "${JUMP_KEY_FILE}"
    chmod 600 "${JUMP_KEY_FILE}"

    echo "[TUNNEL] Connecting to jump server: ${JUMP_HOST}"

    # Open reverse tunnel in background with auto-restart loop
    # -N        : no remote command
    # -R 2222:  : bind port 2222 on jump server → localhost:22 here
    # -o ...    : keep-alive + no host-key prompt
    (
        while true; do
            echo "[TUNNEL] Opening reverse tunnel to ${JUMP_HOST}:22 ..."
            ssh -N \
                -i "${JUMP_KEY_FILE}" \
                -R 2222:localhost:22 \
                -o StrictHostKeyChecking=no \
                -o UserKnownHostsFile=/dev/null \
                -o ServerAliveInterval=30 \
                -o ServerAliveCountMax=6 \
                -o ExitOnForwardFailure=yes \
                -o LogLevel=ERROR \
                root@"${JUMP_HOST}" 2>>/data/logs/tunnel.log
            echo "[TUNNEL] Connection dropped — reconnecting in 10 s..." \
                >> /data/logs/tunnel.log
            sleep 10
        done
    ) &
    TUNNEL_PID=$!

    # Wait up to 20 s for the tunnel to come up
    TUNNEL_UP=0
    for _i in $(seq 1 20); do
        sleep 1
        # A successful tunnel leaves an ssh process running
        if kill -0 "${TUNNEL_PID}" 2>/dev/null && \
           [ "$(jobs -r | wc -l)" -gt 0 ]; then
            TUNNEL_UP=1
            break
        fi
    done

    if [ "${TUNNEL_UP}" = "1" ]; then
        echo "[TUNNEL] Reverse tunnel established."
        echo "[TUNNEL] ============================================"
        echo "[TUNNEL] Termius settings:"
        echo "[TUNNEL]   Host     : ${JUMP_HOST}"
        echo "[TUNNEL]   Port     : 2222"
        echo "[TUNNEL]   Username : root"
        echo "[TUNNEL]   Password : your SSH_PASSWORD secret"
        echo "[TUNNEL] ============================================"
    else
        echo "[TUNNEL] WARNING: Could not confirm tunnel in 20 s."
        echo "[TUNNEL] Check /data/logs/tunnel.log for details."
        echo "[TUNNEL] The loop will keep retrying in the background."
        echo "[TUNNEL] ============================================"
    fi

    # Clean up key after use
    rm -f "${JUMP_KEY_FILE}"
fi

# ─── Start DB self-healing daemon ────────────────────────────────────
echo "[CLEANER] Starting self-healing daemon..."
python3 /cleaner.py &

# ─── Start background sync daemon ────────────────────────────────────
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
