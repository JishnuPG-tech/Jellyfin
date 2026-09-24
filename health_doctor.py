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
    storage volume. Requires APEX_JELLYFIN_API_KEY (admin token) to be set.
    Returns True once all expected libraries exist.
    """
    auth = _jellyfin_auth_header()
    if not auth:
        logger.info("[JELLYFIN] APEX_JELLYFIN_API_KEY not set - skipping library bootstrap.")
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
