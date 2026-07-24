import sqlite3
import time
import os
from datetime import datetime

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

log("OpenCode Self-Healing Daemon: started")

# Priority order: /data is the persistent HF bucket mount
DB_PATHS = [
    "/data/share/opencode/opencode.db",
    "/projects/.opencode/share/opencode/opencode.db",
    "/root/.local/share/opencode/opencode.db",
    "/home/opencode/.local/share/opencode/opencode.db",
]

def is_corrupted(s):
    """Check if a string contains Unicode replacement characters from bad base64 decoding."""
    return s and ("\ufffd" in s or "??#y" in s or "\xef\xbf\xbd" in s)

cycle = 0
while True:
    time.sleep(3)
    cycle += 1

    for db_path in DB_PATHS:
        if not os.path.exists(db_path):
            continue
        try:
            conn = sqlite3.connect(db_path, timeout=5)
            cursor = conn.cursor()

            # Check if session table exists
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='session'")
            if not cursor.fetchone():
                conn.close()
                continue


            # Log session count periodically
            if cycle % 20 == 0:  # Every ~60 seconds
                cursor.execute("SELECT COUNT(*) FROM session")
                count = cursor.fetchone()[0]
                log(f"DB {db_path}: {count} sessions")

            # Fix corrupted directory/path in session table
            cursor.execute("SELECT id, directory, path FROM session")
            for row_id, directory, path in cursor.fetchall():
                need_update = False
                new_directory = directory
                new_path = path

                if is_corrupted(directory):
                    new_directory = "/projects/default"
                    need_update = True

                if is_corrupted(path):
                    new_path = None
                    need_update = True

                if need_update:
                    log(f"[Healed] Session '{row_id}': dir={directory!r} -> {new_directory!r}, path={path!r} -> {new_path!r}")
                    cursor.execute(
                        "UPDATE session SET directory = ?, path = ? WHERE id = ?",
                        (new_directory, new_path, row_id)
                    )
                    conn.commit()

            # Fix corrupted entries in project_directory table
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='project_directory'")
            if cursor.fetchone():
                cursor.execute("SELECT project_id, directory FROM project_directory")
                for p_id, directory in cursor.fetchall():
                    if is_corrupted(directory):
                        log(f"[Healed] project_directory '{p_id}': deleting corrupted dir={directory!r}")
                        cursor.execute(
                            "DELETE FROM project_directory WHERE project_id = ? AND directory = ?",
                            (p_id, directory)
                        )
                        conn.commit()

            conn.close()
        except sqlite3.OperationalError:
            pass  # DB locked by opencode, retry next cycle
        except Exception as e:
            log(f"[Healer Error] {db_path}: {e}")
