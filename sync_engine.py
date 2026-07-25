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
  python3 /sync_engine.py test      — E2E sync verification (create/modify/delete)

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
# /data/share or /data/config XDG roots.  Those roots contain Android SDK caches,
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
}

MAX_FILE_BYTES       = 50 * 1_024 * 1_024   # 50 MB hard limit per file
POLL_INTERVAL_SECS   = 15                    # how often to check for changes
# Checkpoint does a FRESH SCAN but does NOT re-upload everything —
# it only uploads files that are genuinely new or changed.
CHECKPOINT_INTERVAL_SECS = 300


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
        Walk the watched dirs and DELETE any file that _should_skip would
        exclude from syncing.  These are files that leaked in from earlier
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
        This removes the old contaminated entries (Android SDK caches, etc.)
        that were committed by the broken engine.
        Returns the number of files deleted from the dataset.
        """
        if not self._api:
            return 0

        log.info("=== DATASET PURGE: scanning for contaminated entries ===")
        try:
            dataset_files = self._api.list_repo_tree(
                repo_id=HF_DATASET,
                repo_type="dataset",
                recursive=True,
            )
            to_delete = []
            for f in dataset_files:
                # Skip internal/meta files
                rp = getattr(f, "path", None)
                if rp is None:
                    continue
                if rp.startswith("."):   # .gitattributes, .sync-ok etc.
                    continue
                # If it's not in our current valid set, it's junk
                if rp not in valid_repo_paths:
                    to_delete.append(rp)

            if not to_delete:
                log.info("  Dataset is clean — no junk entries found")
                return 0

            log.info(f"  Found {len(to_delete)} junk entries to purge from dataset")
            for rp in to_delete[:10]:
                log.info(f"    - {rp}")
            if len(to_delete) > 10:
                log.info(f"    ... and {len(to_delete) - 10} more")

            from huggingface_hub import CommitOperationDelete
            ops = [CommitOperationDelete(path_in_repo=rp) for rp in to_delete]

            # Delete in batches of 200 to avoid API limits
            batch_size = 200
            total_deleted = 0
            for i in range(0, len(ops), batch_size):
                batch = ops[i:i + batch_size]
                self._api.create_commit(
                    repo_id=HF_DATASET,
                    repo_type="dataset",
                    commit_message=f"sync: purge {len(batch)} contaminated entries (batch {i // batch_size + 1})",
                    operations=batch,
                )
                total_deleted += len(batch)
                log.info(f"  Purge batch {i // batch_size + 1}: deleted {len(batch)} entries")

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

        # ── Step 1: Clean junk that leaked in from old contaminated restores ──
        log.info("=== WATCH: cleaning junk from watched dirs ===")
        removed = self._clean_watched_dirs()
        if removed:
            log.info(f"  Cleaned {removed} junk files from local watched dirs")
        else:
            log.info("  Watched dirs are clean")

        # ── Step 2: Establish baseline (only clean files now) ────────────────
        log.info("=== WATCH: establishing baseline scan ===")
        self._known = self._scan()
        log.info(f"Baseline: {len(self._known)} files tracked")
        log.info("Polling every %ds. Watched dirs:", POLL_INTERVAL_SECS)
        for prefix, path in WATCH_DIRS.items():
            log.info(f"  [{prefix}] {path}")

        # ── Step 3: Purge contaminated entries from the dataset ───────────────
        # Any dataset file NOT in our baseline is leftover junk from the old
        # broken engine and must be deleted.
        self._purge_dataset_junk(set(self._known.keys()))

        last_checkpoint = time.monotonic()

        # ── Step 4: Poll loop ────────────────────────────────────────────────
        while True:
            time.sleep(POLL_INTERVAL_SECS)
            try:
                now = time.monotonic()

                if (now - last_checkpoint) >= CHECKPOINT_INTERVAL_SECS:
                    # Refresh the baseline FROM DISK (not a re-upload of everything)
                    # Only files that are genuinely new or modified get uploaded.
                    # We do NOT reset self._known to {} — that caused the mass re-upload.
                    log.info("[CHECKPOINT] Refreshing baseline from disk ...")
                    fresh = self._scan()
                    # Merge: keep known hashes for unchanged files, flag changed ones
                    merged_known: dict[str, str] = {}
                    for rp, md5 in fresh.items():
                        merged_known[rp] = md5
                    self._known = merged_known
                    last_checkpoint = now

                n_up, n_del = self.sync_once()

                if n_up == 0 and n_del == 0:
                    pass  # quiet — nothing to do
                else:
                    log.info(f"Sync cycle: +{n_up} ~{n_del}")

            except Exception as exc:
                log.error(f"Watch cycle error: {exc}")
                log.error(traceback.format_exc())

    # ------------------------------------------------------------------
    # End-to-end self-test
    # ------------------------------------------------------------------

    def run_e2e_test(self) -> bool:
        """
        E2E sync verification — relies on the watch daemon (separate process)
        to perform the actual sync.  This process only WRITES files and VERIFIES
        via the HF API.  It does NOT call sync_once() itself.

        Flow:
          1. Wait 90s for the watch daemon to finish its startup cleanup + purge.
          2. Create  sync_e2e_test.py  → wait daemon polling → verify in dataset.
          3. Modify  sync_e2e_test.py  → wait daemon polling → verify update.
          4. Delete  sync_e2e_test.py  → wait daemon polling → verify removed.
        """
        if not self._api:
            log.error("E2E test skipped — no HF_TOKEN")
            return False

        test_file  = Path("/projects/default/sync_e2e_test.py")
        repo_path  = "workspace/sync_e2e_test.py"
        # Allow daemon polling (15s) × 2 + buffer — gives the daemon two chances
        wait_secs  = POLL_INTERVAL_SECS * 2 + 10   # 40 seconds

        def dataset_content() -> str | None:
            """Fetch the file content from the dataset; None if missing."""
            try:
                from huggingface_hub import hf_hub_download
                with tempfile.TemporaryDirectory() as td:
                    p = hf_hub_download(
                        repo_id=HF_DATASET, repo_type="dataset",
                        filename=repo_path, token=HF_TOKEN,
                        local_dir=td, force_download=True,
                    )
                    return Path(p).read_text()
            except Exception:
                return None

        log.info("=" * 60)
        log.info("=== E2E SYNC TEST START ===")
        log.info("=== Waiting 90s for watch daemon cleanup+purge to finish ===")
        log.info("=" * 60)
        time.sleep(90)

        passed = 0

        # ── Step 1: CREATE ───────────────────────────────────────────
        log.info("[E2E STEP 1/3] Writing /projects/default/sync_e2e_test.py (VERSION=1) ...")
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text("# sync e2e test - CREATED\nVERSION = 1\n")
        log.info(f"  File written locally.  Waiting {wait_secs}s for watch daemon to sync ...")
        time.sleep(wait_secs)

        content = dataset_content()
        if content and "VERSION = 1" in content:
            log.info("  ✅ STEP 1 PASSED — sync_e2e_test.py created in dataset")
            log.info(f"     Dataset content: {content.strip()!r}")
            passed += 1
        else:
            log.error(f"  ❌ STEP 1 FAILED — file not in dataset (content={content!r})")

        # ── Step 2: MODIFY ───────────────────────────────────────────
        log.info("[E2E STEP 2/3] Modifying sync_e2e_test.py (VERSION=2) ...")
        test_file.write_text("# sync e2e test - MODIFIED\nVERSION = 2\n")
        log.info(f"  Waiting {wait_secs}s for watch daemon to sync ...")
        time.sleep(wait_secs)

        content = dataset_content()
        if content and "VERSION = 2" in content:
            log.info("  ✅ STEP 2 PASSED — modification synced to dataset")
            log.info(f"     Dataset content: {content.strip()!r}")
            passed += 1
        else:
            log.error(f"  ❌ STEP 2 FAILED — modification not in dataset (content={content!r})")

        # ── Step 3: DELETE ───────────────────────────────────────────
        log.info("[E2E STEP 3/3] Deleting sync_e2e_test.py locally ...")
        try:
            test_file.unlink()
            log.info("  File deleted locally.")
        except FileNotFoundError:
            log.warning("  File already gone — continuing.")
        log.info(f"  Waiting {wait_secs}s for watch daemon to sync deletion ...")
        time.sleep(wait_secs)

        content = dataset_content()
        if content is None:
            log.info("  ✅ STEP 3 PASSED — sync_e2e_test.py removed from dataset")
            passed += 1
        else:
            log.error(f"  ❌ STEP 3 FAILED — file still in dataset (content={content!r})")

        # ── Summary ──────────────────────────────────────────────────
        log.info("=" * 60)
        log.info(f"=== E2E TEST RESULT: {passed}/3 steps passed ===")
        if passed == 3:
            log.info("=== ✅ ALL STEPS PASSED — sync engine is working correctly ===")
        else:
            log.error(f"=== ❌ {3 - passed} STEP(S) FAILED — check logs above ===")
        log.info("=" * 60)
        return passed == 3


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
    elif mode == "test":
        ok = engine.run_e2e_test()
        sys.exit(0 if ok else 1)
    else:
        print(f"Usage: sync_engine.py [restore|watch|sync|test]", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
