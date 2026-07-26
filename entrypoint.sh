#!/bin/sh
# OpenCode-Serve entrypoint
#
#  nginx    :7860  (HF exposed)
#    /terminal  → ttyd  :7681  — real PTY bash, mobile-optimised
#    /          → opencode :8080 — chat UI + REST API + SSE
#  sshd     :22   (internal only)
#    ↑ exposed via bore transparent TCP tunnel
#      Termius → bore.pub:PORT → raw TCP → sshd:22
#      (no SSH interception = full PTY, arrow keys, Ctrl+C, vim)
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

# SSH banner — shown immediately on connect, before any shell
cat > /etc/ssh/banner << 'BANNER'

  ╔══════════════════════════════════════╗
  ║      OpenCode SSH — connected        ║
  ║  Type commands normally. Have fun!   ║
  ╚══════════════════════════════════════╝

BANNER

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
Banner /etc/ssh/banner
AcceptEnv LANG LC_* TERM COLORTERM
Subsystem sftp /usr/lib/openssh/sftp-server
SSHD_CONF

# Pin root's shell to bash directly in /etc/passwd (most reliable)
sed -i 's|^root:x:0:0:root:/root:.*|root:x:0:0:root:/root:/bin/bash|' /etc/passwd
echo "[SSH] root shell: $(grep ^root /etc/passwd | cut -d: -f7)"

# Write .bashrc — sourced for interactive non-login shells
cat > /root/.bashrc << 'BASHRC'
export PS1='\[\e[32m\]\u@opencode\[\e[0m\]:\[\e[34m\]\w\[\e[0m\]\$ '
export TERM="${TERM:-xterm-256color}"
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
alias ll='ls -la --color=auto'
alias ls='ls --color=auto'
echo ""
echo "  OpenCode workspace: /projects/default"
echo "  Type 'opencode' to launch the TUI, or work normally."
echo ""
cd /projects/default 2>/dev/null || true
BASHRC

# Write .bash_profile — sourced for login shells (SSH always uses this)
cat > /root/.bash_profile << 'BASH_PROFILE'
# SSH login shell entry point
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
[ -f /root/.bashrc ] && source /root/.bashrc
BASH_PROFILE

chmod 644 /root/.bashrc /root/.bash_profile /etc/ssh/banner

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

# ─── Transparent TCP tunnel via bore ─────────────────────────────────
# bore forwards raw TCP bytes without touching the SSH protocol.
# Termius → bore.pub:PORT → raw TCP → sshd:22
# The SSH handshake happens directly between Termius and sshd — no
# middleman, so PTY allocation, arrow keys, Ctrl+C, vim all work.
#
BORE_LOG=/data/logs/bore.log
TUNNEL_HOST=""
TUNNEL_PORT=""
BORE_PID=""

echo "[TUNNEL] Starting transparent TCP tunnel via bore..."
> "${BORE_LOG}"

bore local 22 --to bore.pub > "${BORE_LOG}" 2>&1 &
BORE_PID=$!

# Wait up to 20s for bore to print the assigned port
for _i in $(seq 1 20); do
    sleep 1
    TUNNEL_PORT=$(grep -oE 'bore\.pub:[0-9]+' "${BORE_LOG}" 2>/dev/null \
        | grep -oE '[0-9]+$' | head -1)
    [ -n "${TUNNEL_PORT}" ] && break
done

if [ -n "${TUNNEL_PORT}" ] && kill -0 "${BORE_PID}" 2>/dev/null; then
    TUNNEL_HOST="bore.pub"
fi

echo "[TUNNEL] ============================================"
if [ -n "${TUNNEL_HOST}" ]; then
    echo "[TUNNEL] Tunnel type  : bore (transparent TCP — no SSH interception)"
    echo "[TUNNEL] Status       : UP"
    echo "[TUNNEL] ----"
    echo "[TUNNEL] Termius host : ${TUNNEL_HOST}"
    echo "[TUNNEL] Termius port : ${TUNNEL_PORT}"
    echo "[TUNNEL] Termius user : root"
    echo "[TUNNEL] Termius pass : (your SSH_PASSWORD secret)"
    echo "[TUNNEL] ----"
    echo "[TUNNEL] PTY support  : FULL (arrow keys, Ctrl+C, vim, tab completion)"
    echo "[TUNNEL] Tunnel log   : ${BORE_LOG}"
    echo "[TUNNEL] ============================================"
    echo "bore.pub:${TUNNEL_PORT}" > /data/logs/tunnel-url.txt

    # Auto-restart: if bore dies, reconnect and log the new port
    (
        _bore_pid="${BORE_PID}"
        while true; do
            sleep 10
            if ! kill -0 "${_bore_pid}" 2>/dev/null; then
                _ts="$(date -u '+%H:%M:%S')"
                echo "[TUNNEL][${_ts}] bore dropped — reconnecting..." >> "${BORE_LOG}"
                bore local 22 --to bore.pub >> "${BORE_LOG}" 2>&1 &
                _bore_pid=$!
                # Wait for new port
                for _w in $(seq 1 15); do
                    sleep 1
                    _new_port=$(grep -oE 'bore\.pub:[0-9]+' "${BORE_LOG}" 2>/dev/null \
                        | grep -oE '[0-9]+$' | tail -1)
                    [ -n "${_new_port}" ] && break
                done
                if [ -n "${_new_port}" ]; then
                    echo "[TUNNEL][$(date -u '+%H:%M:%S')] Reconnected: bore.pub:${_new_port}" >> "${BORE_LOG}"
                    echo "bore.pub:${_new_port}" > /data/logs/tunnel-url.txt
                fi
            fi
        done
    ) &
else
    echo "[TUNNEL] FAILED — bore could not connect to bore.pub"
    echo "[TUNNEL] Bore output:"
    cat "${BORE_LOG}" 2>/dev/null | sed 's/^/[TUNNEL]   /'
    echo "[TUNNEL] Terminal at /terminal still works."
    echo "[TUNNEL] ============================================"
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
