#!/bin/sh
# OpenCode-Serve entrypoint
#
#  nginx    :7860  (HF exposed)
#    /terminal  → ttyd  :7681  — real PTY bash, mobile-optimised
#    /          → opencode :8080 — chat UI + REST API + SSE
#  sshd     :22   (internal only)
#    ↑ exposed via reverse SSH tunnel through serveo.net
#      Termius → opencode-hf.serveo.net:22 → here
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
echo "[SSH] Configuring sshd..."

cat > /etc/ssh/sshd_config << 'SSHD_CONF'
Port 22
PermitRootLogin yes
PasswordAuthentication yes
PubkeyAuthentication yes
AuthorizedKeysFile /root/.ssh/authorized_keys
ChallengeResponseAuthentication no
KbdInteractiveAuthentication no
UsePAM no
X11Forwarding no
PrintMotd no
PermitTTY yes
AllowTcpForwarding yes
AcceptEnv LANG LC_* TERM
Subsystem sftp /usr/lib/openssh/sftp-server
SSHD_CONF

# Ensure root uses bash as login shell
chsh -s /bin/bash root 2>/dev/null || true

# Minimal .bashrc so interactive sessions get a working prompt
cat > /root/.bashrc << 'BASHRC'
export PS1='\u@opencode:\w\$ '
export TERM=${TERM:-xterm-256color}
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
alias ll='ls -la'
cd /projects/default 2>/dev/null || true
BASHRC

# Minimal .bash_profile that sources .bashrc
cat > /root/.bash_profile << 'BASH_PROFILE'
[ -f /root/.bashrc ] && source /root/.bashrc
BASH_PROFILE

if [ -n "${SSH_PASSWORD:-}" ]; then
    echo "root:${SSH_PASSWORD}" | chpasswd
    echo "[SSH] Password set from SSH_PASSWORD secret."
else
    GENERATED_PW=$(head -c 16 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 16)
    echo "root:${GENERATED_PW}" | chpasswd
    echo "[SSH] ============================================"
    echo "[SSH] No SSH_PASSWORD secret. Generated: ${GENERATED_PW}"
    echo "[SSH] Set SSH_PASSWORD in Space Secrets to make it permanent."
    echo "[SSH] ============================================"
fi

ssh-keygen -A 2>/dev/null || true
/usr/sbin/sshd
echo "[SSH] sshd running on port 22."

# ─── Reverse SSH tunnel via serveo.net ───────────────────────────────
# serveo.net is a free relay — no account, no binary, no tokens.
# We open an outbound SSH connection (HF allows this) and serveo
# exposes the tunnel at a public hostname:port.
#
# Two attempts:
#   1. Named subdomain: opencode-hf.serveo.net:22  (fixed, easy to remember)
#   2. Random port:     serveo.net:NNNNN           (fallback)
#
SERVEO_LOG=/data/logs/serveo.log
TUNNEL_HOST=""
TUNNEL_PORT=""

echo "[TUNNEL] Starting SSH tunnel via serveo.net..."

# Attempt 1 — named subdomain (opencode-hf.serveo.net:22)
ssh -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=6 \
    -o LogLevel=ERROR \
    -R opencode-hf:22:localhost:22 \
    serveo.net > "${SERVEO_LOG}" 2>&1 &
SERVEO_PID=$!

sleep 8

# Check if named subdomain was accepted
if kill -0 "${SERVEO_PID}" 2>/dev/null; then
    # Process still running — tunnel likely up
    TUNNEL_HOST="opencode-hf.serveo.net"
    TUNNEL_PORT="22"
else
    # Named subdomain taken or serveo refused — try random port
    echo "[TUNNEL] Named subdomain unavailable, trying random port..."
    ssh -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        -o ServerAliveInterval=30 \
        -o ServerAliveCountMax=6 \
        -o LogLevel=INFO \
        -R 0:localhost:22 \
        serveo.net > "${SERVEO_LOG}" 2>&1 &
    SERVEO_PID=$!
    sleep 8

    # Parse assigned port from log
    TUNNEL_PORT=$(grep -oE 'Allocated port [0-9]+' "${SERVEO_LOG}" 2>/dev/null \
        | grep -oE '[0-9]+' | head -1)
    if [ -n "${TUNNEL_PORT}" ]; then
        TUNNEL_HOST="serveo.net"
    fi
fi

# Report result
echo "[TUNNEL] ============================================"
if [ -n "${TUNNEL_HOST}" ]; then
    echo "[TUNNEL] SSH tunnel is UP via serveo.net"
    echo "[TUNNEL] ----"
    echo "[TUNNEL] Termius host     : ${TUNNEL_HOST}"
    echo "[TUNNEL] Termius port     : ${TUNNEL_PORT}"
    echo "[TUNNEL] Termius username : root"
    echo "[TUNNEL] Termius password : your SSH_PASSWORD secret"
    echo "[TUNNEL] ----"
    echo "[TUNNEL] Tunnel log : ${SERVEO_LOG}"

    # Auto-restart loop in background if tunnel drops
    (
        while true; do
            sleep 15
            if ! kill -0 "${SERVEO_PID}" 2>/dev/null; then
                echo "[TUNNEL] Tunnel dropped — reconnecting..." >> "${SERVEO_LOG}"
                if [ "${TUNNEL_HOST}" = "opencode-hf.serveo.net" ]; then
                    ssh -o StrictHostKeyChecking=no \
                        -o UserKnownHostsFile=/dev/null \
                        -o ServerAliveInterval=30 \
                        -o ServerAliveCountMax=6 \
                        -o LogLevel=ERROR \
                        -R opencode-hf:22:localhost:22 \
                        serveo.net >> "${SERVEO_LOG}" 2>&1 &
                    SERVEO_PID=$!
                else
                    ssh -o StrictHostKeyChecking=no \
                        -o UserKnownHostsFile=/dev/null \
                        -o ServerAliveInterval=30 \
                        -o ServerAliveCountMax=6 \
                        -o LogLevel=ERROR \
                        -R 0:localhost:22 \
                        serveo.net >> "${SERVEO_LOG}" 2>&1 &
                    SERVEO_PID=$!
                fi
            fi
        done
    ) &
else
    echo "[TUNNEL] WARNING: Could not establish tunnel via serveo.net."
    echo "[TUNNEL] serveo.net may be temporarily down."
    echo "[TUNNEL] Check ${SERVEO_LOG} for details."
    echo "[TUNNEL] Terminal at /terminal still works without it."
fi
echo "[TUNNEL] ============================================"

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
