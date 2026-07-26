#!/usr/bin/env python3
"""
OpenCode Memory Updater
=======================
Reads memory files from the workspace and writes them into the `instructions`
field of OpenCode's config (opencode.json).  OpenCode injects `instructions`
as a system-level prompt for every new conversation — the user never sees it
as a message and never has to mention the files manually.

Usage:
  python3 /memory_updater.py once    — update once then exit (run at startup)
  python3 /memory_updater.py watch   — update once, then poll every INTERVAL seconds

Extension point
---------------
To add more memory sources (PROJECT.md, MEMORY.md, TEAM.md, RULES.md,
AGENTS.md …) uncomment the relevant lines in MEMORY_FILES below.
Each entry is (filename, section_label).  Files that don't exist are silently
skipped — no errors, no stale instructions from missing files.
"""

import hashlib
import json
import sys
import time
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────────
WORKSPACE   = Path("/projects/default")
CONFIG_PATH = Path("/data/config/opencode/opencode.json")

# ── Extension point ───────────────────────────────────────────────────────────
# Add / uncomment entries to include additional memory sources.
# Files are loaded in order; each becomes a section in the system prompt.
MEMORY_FILES: list[tuple[str, str]] = [
    ("CLAUDE.md",   "Project Memory"),
    # ("PROJECT.md",  "Project Context"),
    # ("MEMORY.md",   "Session Notes"),
    # ("TEAM.md",     "Team Guidelines"),
    # ("RULES.md",    "Coding Rules"),
    # ("AGENTS.md",   "Agent Instructions"),
]

# Polling interval for watch mode (seconds)
INTERVAL = 15

# System-prompt preamble injected before the memory content
PREAMBLE = (
    "The following is your persistent project memory. "
    "Read and incorporate it automatically at the start of every conversation "
    "without being asked. "
    "Do not mention that you are reading these files unless directly asked.\n\n"
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    print(f"[MEMORY] {msg}", flush=True)


def _load_memory() -> str:
    """Load all MEMORY_FILES that exist; return combined instructions string."""
    sections: list[str] = []
    for filename, label in MEMORY_FILES:
        fp = WORKSPACE / filename
        if not fp.exists():
            continue
        try:
            content = fp.read_text(encoding="utf-8").strip()
            if content:
                sections.append(f"## {filename} — {label}\n\n{content}")
                _log(f"Loaded {filename} ({len(content):,} chars)")
        except Exception as exc:
            _log(f"Could not read {filename}: {exc}")

    if not sections:
        return ""
    return PREAMBLE + "\n\n---\n\n".join(sections)


def _update_config(instructions: str) -> bool:
    """
    Merge `instructions` into opencode.json (preserving all other fields).
    Returns True when the file was actually changed.
    """
    try:
        d: dict = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    except Exception:
        d = {}

    old = d.get("instructions", "")

    if instructions:
        d["instructions"] = instructions
    else:
        d.pop("instructions", None)

    new = d.get("instructions", "")
    if new == old:
        return False  # nothing changed — don't write

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(d, indent=2), encoding="utf-8")
    return True


def _state_hash() -> str:
    """MD5 of all memory file contents combined (cheap change-detection)."""
    h = hashlib.md5()
    for filename, _ in MEMORY_FILES:
        fp = WORKSPACE / filename
        if fp.exists():
            try:
                h.update(fp.read_bytes())
            except OSError:
                pass
    return h.hexdigest()


# ── Modes ─────────────────────────────────────────────────────────────────────

def once() -> None:
    """Run a single update cycle."""
    instructions = _load_memory()
    changed = _update_config(instructions)
    if instructions:
        _log(f"Instructions ready ({len(instructions):,} chars, changed={changed})")
    else:
        _log("No memory files found — instructions cleared")
        _log("  → Create /projects/default/CLAUDE.md for persistent memory")


def watch() -> None:
    """
    Poll MEMORY_FILES for changes every INTERVAL seconds.
    Runs once immediately, then enters the polling loop.
    """
    _log(f"Watcher started — polling every {INTERVAL}s for changes")
    last_hash: str = ""
    while True:
        try:
            h = _state_hash()
            if h != last_hash:
                last_hash = h
                instructions = _load_memory()
                changed = _update_config(instructions)
                if changed:
                    if instructions:
                        _log(f"Change detected → config updated ({len(instructions):,} chars)")
                    else:
                        _log("Memory files removed → instructions cleared")
        except Exception as exc:
            _log(f"Watch cycle error: {exc}")
        time.sleep(INTERVAL)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "once"
    if mode == "watch":
        once()   # run immediately on start, then enter the polling loop
        watch()
    elif mode == "once":
        once()
    else:
        print(f"Usage: memory_updater.py [once|watch]", file=sys.stderr)
        sys.exit(1)
