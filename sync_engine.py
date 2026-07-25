#!/usr/bin/env python3
"""
OpenCode Sync Engine
====================
Persists /projects/default (workspace), /data/share (OpenCode DB),
and /data/config (OpenCode config) to a private HF Dataset repo.

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
import shutil
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [SYNC] %(message)s",
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

# Local dirs → dataset path prefixes
WATCH_DIRS: dict[str, Path] = {
    "workspace": Path("/projects/default"),
    "share":     Path("/data/share"),
    "config":    Path("/data/config"),
}

# Files/dirs to never sync
IGNORE_NAMES: set[str] = {
    ".git", "node_modules", "__pycache__", ".cache",
    ".npm", ".yarn", ".pnpm", "dist", "build",
}
IGNORE_SUFFIXES: set[str] = {".tmp", ".sock", ".pid", ".lock", ".pyc"}
MAX_FILE_BYTES = 50 * 1_024 * 1_024   # 50 MB hard limit per file

POLL_INTERVAL_SECS       = 15    # how often to check for changes
CHECKPOINT_INTERVAL_SECS = 300   # forced full sync every 5 min


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _should_skip(path: Path) -> bool:
    """Return True if this file should not be synced."""
    for part in path.parts:
        if part in IGNORE_NAMES:
            return True
    if path.suffix in IGNORE_SUFFIXES:
        return True
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            log.warning(f"Skipping large file ({path.stat().st_size // 1024} KB): {path}")
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
        except ImportError:
            log.error("huggingface_hub not installed — sync disabled")

    # ------------------------------------------------------------------
    # Restore (startup)
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
            return False

    def restore(self) -> None:
        """Download the entire dataset snapshot and restore local dirs."""
        if not self._api:
            log.info("Restore skipped (sync disabled)")
            return

        if not self._ensure_repo():
            log.warning("Skipping restore — dataset repo not accessible")
            return

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
                continue
            try:
                for path in base.rglob("*"):
                    if not path.is_file():
                        continue
                    if _should_skip(path):
                        continue
                    rp = _repo_path(prefix, path, base)
                    result[rp] = _md5(path)
            except Exception as exc:
                log.warning(f"Scan error in {base}: {exc}")
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
                    ops.append(
                        CommitOperationAdd(
                            path_in_repo=rp,
                            path_or_fileobj=str(local),
                        )
                    )
            for rp in to_delete:
                ops.append(CommitOperationDelete(path_in_repo=rp))

            if not ops:
                self._known = current
                return 0, 0

            n_up = len(to_upload)
            n_del = len(to_delete)
            msg = f"sync: +{n_up} ~{n_del}"

            self._api.create_commit(
                repo_id=HF_DATASET,
                repo_type="dataset",
                commit_message=msg,
                operations=ops,
            )

            log.info(f"↑ committed: +{n_up} uploads, -{n_del} deletes → {HF_DATASET}")
            if to_upload[:5]:
                for rp in to_upload[:5]:
                    log.info(f"   + {rp}")
                if n_up > 5:
                    log.info(f"   ... and {n_up - 5} more")
            if to_delete:
                for rp in to_delete[:3]:
                    log.info(f"   - {rp}")

            self._known = current
            return n_up, n_del

        except Exception as exc:
            log.error(f"Commit failed (will retry next cycle): {exc}")
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

        last_checkpoint = time.monotonic()

        while True:
            time.sleep(POLL_INTERVAL_SECS)
            try:
                now = time.monotonic()
                forced = (now - last_checkpoint) >= CHECKPOINT_INTERVAL_SECS

                if forced:
                    # Force a full rescan to catch anything missed
                    self._known = {}
                    log.info("[CHECKPOINT] Forced full sync")

                n_up, n_del = self.sync_once()

                if forced:
                    last_checkpoint = now
                    if n_up == 0 and n_del == 0:
                        log.info("[CHECKPOINT] Nothing changed — workspace in sync")

            except Exception as exc:
                log.error(f"Watch cycle error: {exc}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "watch"

    # Ensure all local dirs exist (they may not if /data was previously a volume mount)
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
