#!/bin/sh
# OpenCode-Serve entrypoint — direct mode (no proxy):
#  * opencode serve on :7860  — Chat UI works directly on the HF port
#  * ttyd on :7681 (internal) — embedded terminal inside the container
set -u

echo "============================================"
echo "=== OpenCode-Serve starting (direct mode)  ==="
echo "Time: $(date)"
echo "============================================"

mkdir -p /data/share/opencode /data/config/opencode /data/cache/opencode /data/state/opencode \
         /data/workspaces /data/logs \
 2>/dev/null || true

# ─── Detect and remove malformed SQLite databases ───
echo "[DB] Checking database integrity..."
DB_PATHS="/data/share/opencode/opencode.db \
          /projects/.opencode/share/opencode/opencode.db \
          /root/.local/share/opencode/opencode.db \
          /home/opencode/.local/share/opencode/opencode.db"

for db_path in $DB_PATHS; do
  if [ -f "$db_path" ]; then
    # Use sqlite3 to run integrity_check; if it fails or returns non-ok, delete the DB
    result=$(python3 -c "
import sqlite3, sys
try:
    conn = sqlite3.connect('$db_path', timeout=3)
    row = conn.execute('PRAGMA integrity_check').fetchone()
    conn.close()
    print(row[0] if row else 'error')
except Exception as e:
    print('error: ' + str(e))
" 2>/dev/null || echo "error")
    if [ "$result" != "ok" ]; then
      echo "[DB] Malformed database detected at $db_path (result: $result) — removing."
      rm -f "$db_path" "${db_path}-wal" "${db_path}-shm" 2>/dev/null || true
    else
      echo "[DB] Database OK: $db_path"
    fi
  fi
done

if [ ! -f /data/config/opencode/opencode.json ]; then
  mkdir -p /data/config/opencode
  python3 <<'PYEOF' 2>/dev/null || true
import json
d = {"$schema":"https://opencode.ai/config.json","server":{"port":4096,"hostname":"127.0.0.1"},"model":"opencode/big-pickle"}
json.dump(d, open("/data/config/opencode/opencode.json","w"), indent=2)
PYEOF
fi

mkdir -p /projects/default
cd /projects/default
[ -d .git ] || git init -q 2>/dev/null

# ─── ttyd on 0.0.0.0:7681 (internal, optional) ───
echo "[TERMINAL] ttyd on 0.0.0.0:7681 ..."
nohup ttyd -p 7681 -i 0.0.0.0 -W \
  bash -l > /data/logs/ttyd.log 2>&1 &

# ─── opencode serve on 7860 (HF exposed, foreground) ───
echo "[UPSTREAM] opencode serve on :${PORT:-7860} ..."
exec opencode serve --port "${PORT:-7860}" --hostname 0.0.0.0
