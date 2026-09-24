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
import shutil
import glob
import sqlite3
import logging

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
    while True:
        try:
            run_health_check_cycle()
        except Exception as exc:
            logger.error(f"Error in health check loop: {exc}")
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
