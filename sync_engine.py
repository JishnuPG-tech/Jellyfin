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

# ─── CRITICAL FIX ────────────────────────────────────────────────────────────
# Scope watched dirs to the SPECIFIC OpenCode subdirectories, NOT the entire
# /data/share or /data/config XDG roots. Those roots contain Android SDK caches,
# sdkbin-*, sdkinf-* files, npm/pip caches, etc. — thousands of unrelated
# system files that must never be synced.
#
#   "workspace" → /projects/default          (the user's actual project files)
#   "share"     → /data/share/opencode       (OpenCode SQLite DB only)
#   "config"    → /data/config/opencode      (OpenCode config JSON only)
# ─────────────────────────────────────────────────────────────────────────────
WATCH_DIRS: dict[str, Path] = {
    "workspace": Path("/projects/default"),
    "share":     Path("/data/share/opencode"),   # was /data/share — TOO BROAD
    "config":    Path("/data/config/opencode"),  # was /data/config — TOO BROAD
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
    # Android / SDK
    ".android",
    ".gradle",
    ".kotlin",
    "sdktools",
    # Temp / logs / misc
    "tmp",
    "temp",
    "logs",
    ".local",     # catches ~/.local caches outside our watched subdirs
}

# Name *prefixes* to skip (catches sdkbin-*, sdkinf-*, tmp-*, etc.)
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
}

MAX_FILE_BYTES = 50 * 1_024 * 1_024   # 50 MB hard limit per file

POLL_INTERVAL_SECS       = 15    # how often to check for changes
CHECKPOINT_INTERVAL_SECS = 300   # forced full sync every 5 min


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _should_skip(path: Path) -> bool:
    """Return True if this file should not be synced."""
    # Check every path component
    for part in path.parts:
        if part in IGNORE_NAMES:
            log.debug(f"Skip (ignored name '{part}'): {path}")
            return True
        # Prefix check (sdkbin-*, sdkinf-*, etc.)
        for pfx in IGNORE_PREFIXES:
            if part.startswith(pfx):
                log.debug(f"Skip (ignored prefix '{pfx}'): {path}")
                return True

    # Suffix check
    if path.suffix in IGNORE_SUFFIXES:
        log.debug(f"Skip (ignored suffix '{path.suffix}'): {path}")
        return True

    # Size check
    try:
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            log.warning(f"Skip (too large {size // 1024} KB): {path}")
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
        self._api = None
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
            log.info(f"Watching directories:")
            for prefix, path in WATCH_DIRS.items():
                log.info(f"  [{prefix}] {path}")
        except ImportError:
            log.error("huggingface_hub not installed — sync disabled")

    # ------------------------------------------------------------------
    # Repo setup
    # ------------------------------------------------------------------

    def _ensure_repo(self) -> bool:
        """Create the dataset repo if it does not exist yet. Returns True on success."""
        try:
            self._api.create_repo(
                repo_id=HF_DATASET,
                repo_type="dataset",
                private=True,
                exist_ok=True,
            )
            return True
        except Exception as exc:
            log.error(
                f"Could not create/access dataset repo {HF_DATASET}: {exc}\n"
                "  → Create it manually at https://huggingface.co/new-dataset "
                "(name: OpenCode-Storage, private) then restart the Space."
            )
            log.error(traceback.format_exc())
            return False

    def _test_write(self) -> bool:
        """Upload a tiny sentinel file to verify write access. Logs clearly on failure."""
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
            log.error("=" * 60)
            log.error("❌ SYNC WRITE FAILED — files will NOT be saved to dataset")
            log.error(f"   Error: {exc}")
            log.error(traceback.format_exc())
            log.error("   Fix: go to https://huggingface.co/settings/tokens")
            log.error("   Create a token with 'Write' scope (not Read-only).")
            log.error("   Then update HF_TOKEN in Space Settings → Secrets.")
            log.error("=" * 60)
            return False

    # ------------------------------------------------------------------
    # Restore (startup)
    # ------------------------------------------------------------------

    def restore(self) -> None:
        """Download the entire dataset snapshot and restore local dirs."""
        if not self._api:
            log.info("Restore skipped (sync disabled)")
            return

        if not self._ensure_repo():
            log.warning("Skipping restore — dataset repo not accessible")
            return

        # Verify write access immediately so errors are visible in Space logs
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
            # Empty repo or network error — not fatal, start fresh
            log.warning(f"Restore skipped or partial: {exc}")
            log.warning(traceback.format_exc())

    def _copy_snapshot(self, repo_root: Path) -> int:
        """Copy files from a local snapshot dir to their actual locations."""
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
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(src_file, dest_file)
                    log.info(f"  Restored: {prefix}/{rel} → {dest_file}")
                    count += 1
                except Exception as exc:
                    log.warning(f"  Could not restore {dest_file}: {exc}")
        return count

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def _scan(self) -> dict[str, str]:
        """Scan all watched dirs; return {repo_path: md5}."""
        result: dict[str, str] = {}
        for prefix, base in WATCH_DIRS.items():
            if not base.exists():
                log.debug(f"Watched dir does not exist yet: {base}")
                continue
            try:
                file_count = 0
                skip_count = 0
                for path in base.rglob("*"):
                    if not path.is_file():
                        continue
                    if _should_skip(path):
                        skip_count += 1
                        continue
                    rp = _repo_path(prefix, path, base)
                    result[rp] = _md5(path)
                    file_count += 1
                log.debug(f"Scan [{prefix}] {base}: {file_count} tracked, {skip_count} skipped")
            except Exception as exc:
                log.warning(f"Scan error in {base}: {exc}")
                log.warning(traceback.format_exc())
        return result

    # ------------------------------------------------------------------
    # Sync (one shot)
    # ------------------------------------------------------------------

    def sync_once(self) -> tuple[int, int]:
        """Detect changes and push them to the dataset in one commit."""
        if not self._api:
            return 0, 0

        current = self._scan()

        # Determine what changed
        to_upload: list[str] = []
        to_delete: list[str] = []

        for rp, md5 in current.items():
            if self._known.get(rp) != md5:
                to_upload.append(rp)

        for rp in self._known:
            if rp not in current:
                to_delete.append(rp)

        if not to_upload and not to_delete:
            self._known = current
            return 0, 0

        # Build commit operations
        try:
            from huggingface_hub import CommitOperationAdd, CommitOperationDelete

            ops = []
            for rp in to_upload:
                local = _local_path(rp)
                if local and local.exists():
                    log.info(f"  UPLOAD ← {local}  →  {HF_DATASET}/{rp}")
                    ops.append(
                        CommitOperationAdd(
                            path_in_repo=rp,
                            path_or_fileobj=str(local),
                        )
                    )
                else:
                    log.warning(f"  UPLOAD skipped (file vanished): {rp}")

            for rp in to_delete:
                log.info(f"  DELETE from dataset: {rp}")
                ops.append(CommitOperationDelete(path_in_repo=rp))

            if not ops:
                self._known = current
                return 0, 0

            n_up = sum(1 for op in ops if not isinstance(op, __import__("huggingface_hub").CommitOperationDelete))
            n_del = sum(1 for op in ops if isinstance(op, __import__("huggingface_hub").CommitOperationDelete))
            msg = f"sync: +{n_up} ~{n_del}"

            log.info(f"Creating commit '{msg}' on {HF_DATASET} ({len(ops)} operations) ...")
            commit_info = self._api.create_commit(
                repo_id=HF_DATASET,
                repo_type="dataset",
                commit_message=msg,
                operations=ops,
            )
            log.info(f"✅ Commit created: {commit_info.commit_url if hasattr(commit_info, 'commit_url') else 'ok'}")
            log.info(f"↑ Pushed: +{n_up} uploads, -{n_del} deletes → {HF_DATASET}")

            # Update known state after successful commit
            self._known = current
            return len(to_upload), len(to_delete)

        except Exception as exc:
            log.error(f"❌ Commit FAILED (will retry next cycle): {exc}")
            log.error(traceback.format_exc())
            # Don't update _known so we retry next cycle
            return 0, 0

    # ------------------------------------------------------------------
    # Watch loop (daemon)
    # ------------------------------------------------------------------

    def watch(self) -> None:
        """Poll for changes indefinitely."""
        if not self._api:
            log.info("Watch loop running in no-op mode (HF_TOKEN not set)")
            while True:
                time.sleep(3_600)

        # Establish baseline without uploading (what's already on disk = already synced)
        log.info("=== WATCH: establishing baseline scan ===")
        self._known = self._scan()
        log.info(f"Baseline: {len(self._known)} files tracked")
        log.info(f"Polling every {POLL_INTERVAL_SECS}s for changes in:")
        for prefix, path in WATCH_DIRS.items():
            log.info(f"  [{prefix}] {path}")

        last_checkpoint = time.monotonic()

        while True:
            time.sleep(POLL_INTERVAL_SECS)
            try:
                now = time.monotonic()
                forced = (now - last_checkpoint) >= CHECKPOINT_INTERVAL_SECS

                if forced:
                    # Force a full rescan to catch anything missed
                    self._known = {}
                    log.info("[CHECKPOINT] Forced full sync — clearing known state")

                n_up, n_del = self.sync_once()

                if forced:
                    last_checkpoint = now
                    if n_up == 0 and n_del == 0:
                        log.info("[CHECKPOINT] Nothing changed — workspace in sync")

            except Exception as exc:
                log.error(f"Watch cycle error: {exc}")
                log.error(traceback.format_exc())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "watch"

    # Ensure all local dirs exist
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
