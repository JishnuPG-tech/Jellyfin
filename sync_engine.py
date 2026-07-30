#!/usr/bin/env python3
"""
OpenCode Sync Engine
====================
Persists /projects/default (workspace), /data/share/opencode (OpenCode DB),
and /data/config/opencode (OpenCode config) to a private HF Dataset repo.

Usage:
  python3 /sync_engine.py restore   — pull dataset → local dirs at startup
  python3 /sync_engine.py watch     — background sync daemon
  python3 /sync_engine.py sync      — one-shot sync then exit

Environment variables (set as Space Secrets):
  HF_TOKEN      — HuggingFace token with read+write access to the dataset
  HF_DATASET    — dataset repo id (default: Jishnupg/OpenCode-Storage)
"""

import sys
import os
import time
import hashlib
import logging
import traceback
import shutil
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [SYNC] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
    force=True,
)
log = logging.getLogger("sync")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
HF_TOKEN   = os.environ.get("HF_TOKEN", "")
HF_DATASET = os.environ.get("HF_DATASET", "Jishnupg/OpenCode-Storage")

# ─── Watched directories ─────────────────────────────────────────────────────
# Scoped to the SPECIFIC OpenCode subdirectories only.
# The XDG roots /data/share and /data/config contain Android SDK caches,
# nvm installations, sdkbin-*/sdkinf-* files and other system noise —
# none of that should ever be synced.
#
#   "workspace" → /projects/default          (user's project files)
#   "share"     → /data/share/opencode       (OpenCode SQLite DB)
#   "config"    → /data/config/opencode      (OpenCode config JSON)
# ─────────────────────────────────────────────────────────────────────────────
WATCH_DIRS: dict[str, Path] = {
    "workspace": Path("/projects/default"),
    "share":     Path("/data/share/opencode"),
    "config":    Path("/data/config/opencode"),
}

# Exact directory-name segments to skip anywhere in the path
IGNORE_NAMES: set[str] = {
    ".git",
    "node_modules",
    "__pycache__",
    ".cache",
    ".npm",
    ".yarn",
    ".pnpm",
    ".pip",
    "dist",
    "build",
    # Databases (ephemeral - auto-initialized on instance)
    "opencode.db",
    "opencode.db-wal",
    "opencode.db-shm",
    # Android / SDK
    ".android",
    ".gradle",
    ".kotlin",
    "sdktools",
    # Temp / logs / misc
    "tmp",
    "temp",
    "logs",
    ".local",
}

# Name *prefixes* to skip (catches sdkbin-*, sdkinf-*, etc.)
IGNORE_PREFIXES: tuple[str, ...] = (
    "sdkbin-",
    "sdkinf-",
    "tmp-",
    "temp-",
    ".~",
)

# File suffixes to skip
IGNORE_SUFFIXES: set[str] = {
    ".tmp", ".sock", ".pid", ".lock", ".pyc",
    ".log", ".bak", ".swp", ".swo",
    ".db-wal", ".db-shm",
}

MAX_FILE_BYTES          = 50 * 1_024 * 1_024   # 50 MB hard limit per file
POLL_INTERVAL_SECS      = 15                    # how often to check for changes
CHECKPOINT_INTERVAL_SECS = 300                  # full-disk re-scan interval


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _should_skip(path: Path) -> bool:
    """Return True if this file should not be synced."""
    for part in path.parts:
        if part in IGNORE_NAMES:
            return True
        for pfx in IGNORE_PREFIXES:
            if part.startswith(pfx):
                return True
    if path.suffix in IGNORE_SUFFIXES:
        return True
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            log.warning(f"Skip (too large): {path}")
            return True
    except OSError:
        return True
    return False


def _md5(path: Path) -> str:
    h = hashlib.md5()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65_536), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _repo_path(prefix: str, local_path: Path, base: Path) -> str:
    """Convert a local absolute path to its dataset repo path."""
    return f"{prefix}/{local_path.relative_to(base)}"


def _local_path(repo_file: str) -> Path | None:
    """Convert a dataset repo path back to a local absolute path."""
    parts = repo_file.split("/", 1)
    if len(parts) < 2:
        return None
    prefix, rel = parts
    base = WATCH_DIRS.get(prefix)
    if base is None:
        return None
    return base / rel


# ---------------------------------------------------------------------------
# SyncEngine
# ---------------------------------------------------------------------------

class SyncEngine:
    def __init__(self) -> None:
        self._api  = None
        self._known: dict[str, str] = {}   # repo_path → md5

        if not HF_TOKEN:
            log.warning(
                "HF_TOKEN not set — sync is disabled.\n"
                "Add HF_TOKEN to your Space Secrets to enable persistent storage."
            )
            return

        try:
            from huggingface_hub import HfApi
            self._api = HfApi(token=HF_TOKEN)
            log.info(f"Sync engine ready → {HF_DATASET}")
            log.info("Watching directories:")
            for prefix, path in WATCH_DIRS.items():
                log.info(f"  [{prefix}] → {path}")
        except ImportError:
            log.error("huggingface_hub not installed — sync disabled")

    # ------------------------------------------------------------------
    # Repo helpers
    # ------------------------------------------------------------------

    def _ensure_repo(self) -> bool:
        try:
            self._api.create_repo(
                repo_id=HF_DATASET,
                repo_type="dataset",
                private=True,
                exist_ok=True,
            )
            return True
        except Exception as exc:
            log.error(f"Could not create/access dataset repo {HF_DATASET}: {exc}")
            log.error(traceback.format_exc())
            return False

    def _test_write(self) -> bool:
        try:
            from huggingface_hub import CommitOperationAdd
            import io
            self._api.create_commit(
                repo_id=HF_DATASET,
                repo_type="dataset",
                commit_message="sync: write-access test",
                operations=[CommitOperationAdd(
                    path_in_repo=".sync-ok",
                    path_or_fileobj=io.BytesIO(b"ok"),
                )],
            )
            log.info("✅ Write access confirmed — dataset sync is active")
            return True
        except Exception as exc:
            log.error("❌ SYNC WRITE FAILED — files will NOT be saved to dataset")
            log.error(f"   Error: {exc}")
            log.error(traceback.format_exc())
            return False

    # ------------------------------------------------------------------
    # Restore (startup)
    # ------------------------------------------------------------------

    def restore(self) -> None:
        if not self._api:
            log.info("Restore skipped (sync disabled)")
            return

        if not self._ensure_repo():
            log.warning("Skipping restore — dataset repo not accessible")
            return

        self._test_write()

        log.info(f"=== RESTORE: pulling {HF_DATASET} ===")
        try:
            from huggingface_hub import snapshot_download
            with tempfile.TemporaryDirectory(prefix="hf_restore_") as tmp:
                local_repo = snapshot_download(
                    repo_id=HF_DATASET,
                    repo_type="dataset",
                    token=HF_TOKEN,
                    local_dir=tmp,
                    local_dir_use_symlinks=False,
                    ignore_patterns=["*.gitattributes", ".gitattributes"],
                )
                count = self._copy_snapshot(Path(local_repo))
            log.info(f"=== RESTORE complete: {count} files ===")
        except Exception as exc:
            log.warning(f"Restore skipped or partial: {exc}")
            log.warning(traceback.format_exc())

    def _copy_snapshot(self, repo_root: Path) -> int:
        """
        Copy files from a local snapshot dir to their actual locations.
        Only restores files that would NOT be skipped by _should_skip, so that
        a previously-contaminated dataset does not poison the watched dirs.
        """
        count = 0
        for prefix, dest_base in WATCH_DIRS.items():
            src_base = repo_root / prefix
            if not src_base.exists():
                log.info(f"  No '{prefix}' in dataset yet — skipping")
                continue
            for src_file in src_base.rglob("*"):
                if not src_file.is_file():
                    continue
                rel = src_file.relative_to(src_base)
                dest_file = dest_base / rel

                # Apply the same skip filter used during scanning —
                # prevents old contaminated dataset entries from coming back
                if _should_skip(dest_file):
                    log.debug(f"  Restore skipped (filtered): {dest_file}")
                    continue

                dest_file.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(src_file, dest_file)
                    log.info(f"  Restored: dataset:{prefix}/{rel} → {dest_file}")
                    count += 1
                except Exception as exc:
                    log.warning(f"  Could not restore {dest_file}: {exc}")
        return count

    # ------------------------------------------------------------------
    # Local junk cleanup
    # ------------------------------------------------------------------

    def _clean_watched_dirs(self) -> int:
        """
        Remove files from the watched dirs that fail the _should_skip filter.
        This is a safety pass to evict any contamination left over from
        contaminated restores (e.g. Android SDK cache files in /data/share).
        Returns the number of files removed.
        """
        removed = 0
        for prefix, base in WATCH_DIRS.items():
            if not base.exists():
                continue
            for path in sorted(base.rglob("*"), reverse=True):  # deep first
                if not path.is_file():
                    continue
                if _should_skip(path):
                    try:
                        path.unlink()
                        log.info(f"  Cleaned junk from watched dir: {path}")
                        removed += 1
                    except Exception as exc:
                        log.warning(f"  Could not clean {path}: {exc}")
        # Remove now-empty dirs (cosmetic, non-fatal)
        for prefix, base in WATCH_DIRS.items():
            if not base.exists():
                continue
            for d in sorted(base.rglob("*"), reverse=True):
                if d.is_dir():
                    try:
                        d.rmdir()  # only succeeds if empty
                    except OSError:
                        pass
        return removed

    # ------------------------------------------------------------------
    # Dataset contamination purge
    # ------------------------------------------------------------------

    def _purge_dataset_junk(self, valid_repo_paths: set[str]) -> int:
        """
        Delete files from the dataset that are NOT in valid_repo_paths.
        Removes any entries that don't correspond to currently tracked local
        files, guarding against dataset contamination from future regressions.
        Returns the number of files deleted from the dataset.
        """
        if not self._api:
            return 0

        log.info("=== DATASET PURGE: scanning for stale entries ===")
        try:
            from huggingface_hub.hf_api import RepoFile
            dataset_files = self._api.list_repo_tree(
                repo_id=HF_DATASET,
                repo_type="dataset",
                recursive=True,
            )
            to_delete = []
            for f in dataset_files:
                if not isinstance(f, RepoFile):
                    continue  # skip directory entries
                rp = f.path
                if rp.startswith("."):   # .gitattributes, .sync-ok etc.
                    continue
                # If it's not in our current valid set, delete it
                if rp not in valid_repo_paths:
                    to_delete.append(rp)

            if not to_delete:
                log.info("  Dataset is clean — no stale entries found")
                return 0

            log.info(f"  Found {len(to_delete)} stale entries to remove from dataset")
            for rp in to_delete[:10]:
                log.info(f"    - {rp}")
            if len(to_delete) > 10:
                log.info(f"    ... and {len(to_delete) - 10} more")

            from huggingface_hub import CommitOperationDelete
            ops = [CommitOperationDelete(path_in_repo=rp) for rp in to_delete]

            # Delete in batches of 200 to stay within API limits
            batch_size = 200
            total_deleted = 0
            for i in range(0, len(ops), batch_size):
                batch = ops[i:i + batch_size]
                bnum = i // batch_size + 1
                self._api.create_commit(
                    repo_id=HF_DATASET,
                    repo_type="dataset",
                    commit_message=f"sync: purge {len(batch)} stale entries (batch {bnum})",
                    operations=batch,
                )
                total_deleted += len(batch)
                log.info(f"  Purge batch {bnum}: deleted {len(batch)} entries")

            log.info(f"=== DATASET PURGE complete: {total_deleted} entries removed ===")
            return total_deleted

        except Exception as exc:
            log.error(f"Dataset purge failed (non-fatal): {exc}")
            log.error(traceback.format_exc())
            return 0

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def _scan(self) -> dict[str, str]:
        """Scan all watched dirs; return {repo_path: md5}."""
        result: dict[str, str] = {}
        for prefix, base in WATCH_DIRS.items():
            if not base.exists():
                continue
            try:
                n_tracked = n_skipped = 0
                for path in base.rglob("*"):
                    if not path.is_file():
                        continue
                    if _should_skip(path):
                        n_skipped += 1
                        continue
                    rp = _repo_path(prefix, path, base)
                    result[rp] = _md5(path)
                    n_tracked += 1
                log.debug(f"Scan [{prefix}] {base}: {n_tracked} tracked, {n_skipped} skipped")
            except Exception as exc:
                log.warning(f"Scan error in {base}: {exc}")
                log.warning(traceback.format_exc())
        return result

    # ------------------------------------------------------------------
    # Sync (one shot)
    # ------------------------------------------------------------------

    def sync_once(self) -> tuple[int, int]:
        """Detect changes vs _known and push them to the dataset."""
        if not self._api:
            return 0, 0

        current = self._scan()

        to_upload = [rp for rp, md5 in current.items() if self._known.get(rp) != md5]
        to_delete  = [rp for rp in self._known if rp not in current]

        if not to_upload and not to_delete:
            self._known = current
            return 0, 0

        try:
            from huggingface_hub import CommitOperationAdd, CommitOperationDelete

            ops: list = []
            for rp in to_upload:
                local = _local_path(rp)
                if local and local.exists():
                    log.info(f"  UPLOAD  local:{local}  →  dataset:{rp}")
                    ops.append(CommitOperationAdd(path_in_repo=rp, path_or_fileobj=str(local)))
                else:
                    log.warning(f"  UPLOAD skipped (file vanished): {rp}")
            for rp in to_delete:
                log.info(f"  DELETE  dataset:{rp}")
                ops.append(CommitOperationDelete(path_in_repo=rp))

            if not ops:
                self._known = current
                return 0, 0

            n_up  = len(to_upload)
            n_del = len(to_delete)
            msg   = f"sync: +{n_up} ~{n_del}"

            log.info(f"Committing '{msg}' → {HF_DATASET} ({len(ops)} ops) ...")
            commit_info = self._api.create_commit(
                repo_id=HF_DATASET,
                repo_type="dataset",
                commit_message=msg,
                operations=ops,
            )
            url = getattr(commit_info, "commit_url", "ok")
            log.info(f"✅ Commit created: {url}")
            log.info(f"↑ Pushed: +{n_up} uploads, -{n_del} deletes → {HF_DATASET}")

            self._known = current
            return n_up, n_del

        except Exception as exc:
            log.error(f"❌ Commit FAILED (will retry next cycle): {exc}")
            log.error(traceback.format_exc())
            return 0, 0

    # ------------------------------------------------------------------
    # Watch loop (daemon)
    # ------------------------------------------------------------------

    def watch(self) -> None:
        if not self._api:
            log.info("Watch loop running in no-op mode (HF_TOKEN not set)")
            while True:
                time.sleep(3_600)

        # Step 1: Establish baseline
        log.info("=== WATCH: establishing baseline scan ===")
        self._known = self._scan()
        log.info(f"Baseline: {len(self._known)} files tracked")
        log.info("Polling every %ds. Watched dirs:", POLL_INTERVAL_SECS)
        for prefix, path in WATCH_DIRS.items():
            log.info(f"  [{prefix}] {path}")

        # Step 3: Purge any stale dataset entries not present in the baseline
        self._purge_dataset_junk(set(self._known.keys()))

        last_checkpoint = time.monotonic()

        # Step 4: Poll loop
        while True:
            time.sleep(POLL_INTERVAL_SECS)
            try:
                now = time.monotonic()

                if (now - last_checkpoint) >= CHECKPOINT_INTERVAL_SECS:
                    # Refresh the baseline FROM DISK; only upload genuinely changed files.
                    # We do NOT reset self._known to {} — that would cause mass re-uploads.
                    log.info("[CHECKPOINT] Refreshing baseline from disk ...")
                    self._known = self._scan()
                    last_checkpoint = now

                n_up, n_del = self.sync_once()

                if n_up or n_del:
                    log.info(f"Sync cycle: +{n_up} ~{n_del}")

            except Exception as exc:
                log.error(f"Watch cycle error: {exc}")
                log.error(traceback.format_exc())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "watch"

    for d in [
        "/data/share/opencode",
        "/data/config/opencode",
        "/data/cache/opencode",
        "/data/state/opencode",
        "/data/logs",
        "/data/workspaces",
        "/projects/default",
    ]:
        Path(d).mkdir(parents=True, exist_ok=True)

    engine = SyncEngine()

    if mode == "restore":
        engine.restore()
    elif mode == "watch":
        engine.watch()
    elif mode == "sync":
        engine.sync_once()
    else:
        print(f"Usage: sync_engine.py [restore|watch|sync]", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
