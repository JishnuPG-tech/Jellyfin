#!/usr/bin/env python3
"""
OpenCode Space — Automated SQLite Health & Backup Doctor
========================================================
Periodically inspects runtime and persistent SQLite database integrity (PRAGMA quick_check),
monitors persistent disk volume usage, and purges expired database backups.
"""

import os
import sys
import time
import json
import uuid
import shutil
import glob
import sqlite3
import logging
import urllib.request
import urllib.error

logger = logging.getLogger("HealthDoctor")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] [%(name)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

JELLYFIN_DB = "/opt/jellyfin-local/data/data/jellyfin.db"
if not os.path.exists(JELLYFIN_DB):
    JELLYFIN_DB = "/data/jellyfin/data/data/jellyfin.db"

TARGET_DATABASES = [
    "/root/.omniroute/storage.sqlite",
    "/data/omniroute/storage.sqlite",
]

BACKUP_DIR = "/data/omniroute/backups"
DATA_DIR = "/data"
RETENTION_DAYS = 5
CHECK_INTERVAL_SECONDS = 300  # 5 minutes


def check_sqlite_integrity(db_path: str) -> bool:
    if not os.path.exists(db_path) or os.path.getsize(db_path) == 0:
        return True

    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("PRAGMA quick_check;")
        res = cur.fetchone()
        conn.close()
        if res and res[0] == "ok":
            return True
        logger.warning(f"⚠️ Quick check failed for {db_path}: {res}")
        return False
    except Exception as exc:
        logger.error(f"❌ Error running quick_check on {db_path}: {exc}")
        return False


def check_disk_space(target_path: str = DATA_DIR):
    try:
        if os.path.exists(target_path):
            total, used, free = shutil.disk_usage(target_path)
            used_pct = (used / total) * 100
            free_mb = free / (1024 * 1024)
            if used_pct > 90:
                logger.warning(f"⚠️ High disk usage on {target_path}: {used_pct:.1f}% used ({free_mb:.1f} MB free)")
            else:
                logger.info(f"📊 Disk usage on {target_path}: {used_pct:.1f}% used ({free_mb:.1f} MB free)")
    except Exception as exc:
        logger.warning(f"Could not inspect disk usage: {exc}")


def purge_old_backups(backup_dir: str = BACKUP_DIR, max_age_days: int = RETENTION_DAYS):
    if not os.path.exists(backup_dir):
        return

    now = time.time()
    cutoff = now - (max_age_days * 86400)
    purged_count = 0

    try:
        files = glob.glob(os.path.join(backup_dir, "storage-*.sqlite"))
        for filepath in files:
            try:
                mtime = os.path.getmtime(filepath)
                if mtime < cutoff:
                    os.remove(filepath)
                    purged_count += 1
            except Exception:
                pass
        if purged_count > 0:
            logger.info(f"🧹 Purged {purged_count} database backup(s) older than {max_age_days} days.")
    except Exception as exc:
        logger.warning(f"Error purging old backups: {exc}")


def _jellyfin_auth_header() -> str:
    """Build a Jellyfin API auth header from the APEX_JELLYFIN_API_KEY secret (if present)."""
    key = os.environ.get("APEX_JELLYFIN_API_KEY", "").strip().strip('"')
    if not key:
        return ""
    return f'MediaBrowser Token="{key}"'


def _jellyfin_virtual_folders(auth):
    """Return (status_code, json) for the library list using the given auth header."""
    url = "http://127.0.0.1:8096/Library/VirtualFolders"
    req = urllib.request.Request(url, headers={"Authorization": auth})
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()
        try:
            return resp.status, json.loads(raw.decode("utf-8"))
        except Exception:
            return resp.status, raw.decode("utf-8", "ignore")


def _read_jellyfin_api_keys():
    """Read every row of Jellyfin's api_keys table (works even when the token is invalid)."""
    db = JELLYFIN_DB
    rows = []
    if not os.path.exists(db):
        return rows
    try:
        with sqlite3.connect(db, timeout=5) as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA table_info('api_keys')")
            columns = [r[1] for r in cur.fetchall()]
            if not columns:
                return rows
            cur.execute("SELECT * FROM 'api_keys'")
            for row in cur.fetchall():
                rows.append({c: v for c, v in zip(columns, row)})
    except Exception as exc:
        logger.warning(f"[JELLYFIN] Could not read api_keys table: {exc}")
    return rows


def _key_is_active(key_row: dict) -> bool:
    """Jellyfin stores IsActive as an INTEGER (0/1); treat None as active for new rows."""
    v = key_row.get("IsActive")
    tok = str(key_row.get("AccessToken") or "")
    if not tok:
        return False
    if v is None:
        return True
    try:
        return int(v) != 0
    except Exception:
        return True


def ensure_active_jellyfin_api_key():
    """Return an auth header backed by a WORKING Jellyfin API key.

    Strategy:
      1. If APEX_JELLYFIN_API_KEY is set and valid, use it.
      2. Otherwise scan Jellyfin's api_keys table for an ACTIVE row and verify it.
      3. As a last resort, try *any* existing row, then insert a fresh active API key
         directly into the database (no admin login needed).
    """
    candidates = []

    env_key = os.environ.get("APEX_JELLYFIN_API_KEY", "").strip().strip('"')
    if env_key:
        candidates.append(("env", env_key, True))

    for row in _read_jellyfin_api_keys():
        tok = str(row.get("AccessToken") or "").strip()
        if not tok:
            continue
        active = _key_is_active(row)
        candidates.append(("db", tok, active))

    tried = set()
    for source, tok, _active in candidates:
        if tok in tried:
            continue
        tried.add(tok)
        auth = f'MediaBrowser Token="{tok}"'
        try:
            status, _ = _jellyfin_virtual_folders(auth)
            if status == 200:
                logger.info(f"[JELLYFIN] Valid API key found via {source}.")
                return auth
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                continue  # invalid/expired token
            logger.warning(f"[JELLYFIN] Key check got HTTP {exc.code} - retrying later.")
            return ""
        except Exception as exc:
            logger.warning(f"[JELLYFIN] Key check error: {exc}")
            return ""

    auth = _insert_jellyfin_api_key()
    if auth:
        logger.info("[JELLYFIN] Created a fresh active API key in the database.")
    return auth


def _insert_jellyfin_api_key():
    """Insert an ACTIVE API key row directly into Jellyfin's sqlite api_keys table."""
    db = JELLYFIN_DB
    if not os.path.exists(db):
        return ""
    token = uuid.uuid4().hex
    try:
        with sqlite3.connect(db, timeout=10) as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA table_info('api_keys')")
            columns = [r[1] for r in cur.fetchall()]
            if not columns:
                logger.warning("[JELLYFIN] api_keys table missing columns - cannot bootstrap.")
                return ""

            if "Id" in columns:
                row_id = uuid.uuid4().hex
            else:
                row_id = None
            payload = {
                "Id": row_id,
                "AppName": "ApexOps",
                "AccessToken": token,
                "UserId": "00000000000000000000000000000000",
                "DateCreated": time.strftime("%Y-%m-%d %H:%M:%S"),
                "IsActive": 1,
            }
            cols = [c for c in columns if c in payload]
            vals = [payload[c] for c in cols]
            placeholders = ",".join("?" * len(vals))
            cur.execute(f"INSERT INTO 'api_keys' ({','.join(cols)}) VALUES ({placeholders})", vals)
            conn.commit()
        return f'MediaBrowser Token="{token}"'
    except Exception as exc:
        logger.warning(f"[JELLYFIN] Could not insert API key: {exc}")
        return ""


def _jellyfin_request(method: str, path: str, auth: str, body=None, timeout: int = 15):
    url = f"http://127.0.0.1:8096{path}"
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = auth
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        try:
            return resp.status, json.loads(raw.decode("utf-8"))
        except Exception:
            return resp.status, raw.decode("utf-8", "ignore")


def ensure_jellyfin_libraries():
    """Idempotently create Movies + TV Shows media libraries if they are missing.

    Runs on a timer, so it also self-heals after `jellyfin/reset` or a fresh
    storage volume. Uses an active API key (env secret or DB-bootstrapped).
    Returns True once all expected libraries exist.
    """
    auth = ensure_active_jellyfin_api_key()
    if not auth:
        logger.info("[JELLYFIN] No working API key available - skipping library bootstrap.")
        return False

    expected = {
        "movies": "/data/jellyfin/media/Movies",
        "tvshows": "/data/jellyfin/media/TV Shows",
    }

    try:
        status, folders = _jellyfin_request("GET", "/Library/VirtualFolders", auth)
    except urllib.error.HTTPError as exc:
        logger.warning(f"[JELLYFIN] Library bootstrap check failed ({exc.code}); Jellyfin may still be booting.")
        return False
    except Exception as exc:
        logger.warning(f"[JELLYFIN] Library bootstrap check error: {exc}")
        return False

    if status != 200 or not isinstance(folders, list):
        logger.warning(f"[JELLYFIN] Library bootstrap unexpected response (status={status}).")
        return False

    existing = {f.get("Name"): f for f in folders if isinstance(f, dict) and f.get("Name")}
    present = set(existing.keys())
    all_present = True

    for collection_type, lib_path in expected.items():
        name = "Movies" if collection_type == "movies" else "TV Shows"
        if name in present:
            continue
        all_present = False

        # Path must exist on disk first, else Jellyfin will reject the location.
        os.makedirs(lib_path, exist_ok=True)

        params = {
            "name": name,
            "collectionType": collection_type,
            "refreshLibrary": "true",
            "paths": lib_path,
        }
        qs = "&".join(f"{k}={urllib.request.quote(str(v))}" for k, v in params.items())
        try:
            status, _ = _jellyfin_request("POST", f"/Library/VirtualFolders?{qs}", auth, body={})
            logger.info(f"[JELLYFIN] Created '{name}' library at {lib_path} (status={status}).")
        except urllib.error.HTTPError as exc:
            logger.warning(f"[JELLYFIN] Could not create '{name}' library ({exc.code}): {exc.read().decode('utf-8', 'ignore')[:200]}")
        except Exception as exc:
            logger.warning(f"[JELLYFIN] Error creating '{name}' library: {exc}")

    return all_present


def run_health_check_cycle():
    logger.info("Starting health & database integrity diagnostic cycle...")
    
    for db_path in TARGET_DATABASES:
        if os.path.exists(db_path):
            ok = check_sqlite_integrity(db_path)
            status = "HEALTHY" if ok else "CORRUPT"
            size_mb = os.path.getsize(db_path) / (1024 * 1024)
            logger.info(f"Database {os.path.basename(db_path)} ({size_mb:.2f} MB): {status}")

    check_disk_space()
    purge_old_backups()
    ensure_jellyfin_libraries()
    try:
        from gateway.credentials_sync import sync_sqlite_credentials_to_vault
        sync_sqlite_credentials_to_vault("/root/.omniroute/storage.sqlite")
    except Exception:
        pass


def main():
    if "--once" in sys.argv:
        run_health_check_cycle()
        return

    logger.info("Health Doctor daemon initialized. Running checks every 5 minutes...")

    # Boot phase: ensure Jellyfin media libraries exist as soon as the server is up.
    boot_deadline = time.time() + 180
    while time.time() < boot_deadline:
        try:
            if ensure_jellyfin_libraries():
                break
        except Exception as exc:
            logger.error(f"Error in boot library bootstrap: {exc}")
        time.sleep(10)

    while True:
        try:
            run_health_check_cycle()
        except Exception as exc:
            logger.error(f"Error in health check loop: {exc}")
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
