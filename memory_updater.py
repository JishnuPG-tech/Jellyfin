#!/usr/bin/env python3
"""
OpenCode Memory Assembly Engine
================================
Assembles structured persistent memory from multiple sources and writes it to
opencode.json's `instructions` field.  OpenCode injects `instructions` as a
system-level prompt for every new conversation — the user never has to type
"read CLAUDE.md" or "continue from yesterday".

Memory sources (assembled in priority order):
  1. memory/GLOBAL.md      — user preferences, tech choices, coding style
  2. memory/PROJECT.md     — architecture, APIs, design decisions, completed work
  3. memory/CONVENTIONS.md — coding rules, naming, patterns
  4. memory/TODO.md        — pending tasks, roadmap
  5. memory/sessions/      — recent conversation summaries (newest first)
  6. Workspace auto-scan   — README, package.json, pyproject.toml, etc.

All memory files live inside /projects/default/memory/ so they are
automatically synced to the HF Dataset by sync_engine.py and survive
every Space rebuild, container recreation, and browser session.

Usage:
  python3 /memory_updater.py once    — assemble once then exit (run at startup)
  python3 /memory_updater.py watch   — assemble, then poll every INTERVAL secs

Extension points:
  Add entries to EXTRA_MEMORY_FILES to include additional sources.
  Add patterns to WORKSPACE_SCAN_FILES to auto-detect more project files.
"""

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
WORKSPACE    = Path("/projects/default")
MEMORY_DIR   = WORKSPACE / "memory"
SESSIONS_DIR = MEMORY_DIR / "sessions"
CONFIG_PATH  = Path("/data/config/opencode/opencode.json")

# ── Character budget ──────────────────────────────────────────────────────────
# Total chars allowed in the `instructions` field.
# Keeps prompts compact while fitting meaningful context.
TOTAL_BUDGET     = 10_000   # total chars budget
GLOBAL_BUDGET    = 1_800    # memory/GLOBAL.md
PROJECT_BUDGET   = 2_800    # memory/PROJECT.md
CONV_BUDGET      = 1_200    # memory/CONVENTIONS.md
TODO_BUDGET      = 1_000    # memory/TODO.md
SESSIONS_BUDGET  = 1_800    # recent session summaries (all combined)
WORKSPACE_BUDGET = 1_200    # auto-scanned workspace context
MAX_SESSIONS     = 5        # most recent session summaries to include

# ── Polling interval ──────────────────────────────────────────────────────────
INTERVAL = 15   # seconds between change-detection polls

# ── Workspace files to auto-scan ──────────────────────────────────────────────
# Scanned in order; first match per category wins.
WORKSPACE_SCAN_FILES: list[tuple[str, str]] = [
    ("README.md",           "Project README"),
    ("README.rst",          "Project README"),
    ("README.txt",          "Project README"),
    ("package.json",        "Node.js Manifest"),
    ("pyproject.toml",      "Python Project"),
    ("requirements.txt",    "Python Dependencies"),
    ("Cargo.toml",          "Rust Manifest"),
    ("go.mod",              "Go Module"),
    ("composer.json",       "PHP Manifest"),
    ("pom.xml",             "Maven Project"),
    ("build.gradle",        "Gradle Project"),
    ("CLAUDE.md",           "Project Instructions"),
    ("AGENTS.md",           "Agent Instructions"),
    (".cursorrules",        "Cursor Rules"),
]

# ── Extra memory sources ──────────────────────────────────────────────────────
# Uncomment or add entries to include additional memory files.
# Each entry: (relative path under MEMORY_DIR, section label, char budget)
EXTRA_MEMORY_FILES: list[tuple[str, str, int]] = [
    # ("TEAM.md",    "Team Guidelines",  800),
    # ("RULES.md",   "Additional Rules", 800),
    # ("AGENTS.md",  "Agent Config",     600),
]

# ── System prompt preamble ────────────────────────────────────────────────────
MEMORY_INSTRUCTIONS = """\
You are an AI coding assistant with a persistent memory system. Your memory \
is loaded automatically at the start of every conversation. You already know \
the project, previous decisions, and user preferences — do not ask the user \
to re-explain them.

MEMORY FILES  (all in /projects/default/memory/)
─────────────────────────────────────────────────
• GLOBAL.md      → user preferences, coding style, preferred tech, reusable workflows
• PROJECT.md     → architecture, APIs, schemas, design decisions, completed features
• CONVENTIONS.md → naming rules, formatting, code patterns
• TODO.md        → pending tasks, roadmap, known issues

HOW TO UPDATE YOUR MEMORY
──────────────────────────
Use your file-editing tools to update the appropriate file when you learn:
  ✓ Architecture decisions or design choices
  ✓ User preferences or workflow patterns
  ✓ APIs, schemas, or important implementation details
  ✓ New tasks or completed work
  ✗ Temporary debugging, casual chat, one-off questions (do NOT save these)

Keep memory files concise. Replace outdated facts; add new ones.

SESSION SUMMARIES
─────────────────
After any meaningful work session, write a summary to:
  /projects/default/memory/sessions/YYYY-MM-DD_HH-MM.md

Use this format:
  # Session YYYY-MM-DD HH:MM
  ## Accomplished
  - ...
  ## Decisions Made
  - ...
  ## Files Modified
  - ...
  ## Next Steps
  - ...

The memory watcher picks up all changes within 15 seconds and injects them \
into the next conversation automatically.
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [MEMORY] {msg}", flush=True)


def _trunc(text: str, budget: int, label: str = "") -> str:
    """Truncate text to budget chars, appending a note if cut."""
    if len(text) <= budget:
        return text
    cut = text[:budget]
    # Try to cut at a newline to avoid mid-sentence truncation
    nl = cut.rfind("\n")
    if nl > budget * 0.7:
        cut = cut[:nl]
    suffix = f"\n\n[... {label} truncated for brevity ...]" if label else "\n\n[... truncated ...]"
    return cut + suffix


def _section(title: str, content: str) -> str:
    bar = "─" * min(len(title) + 4, 60)
    return f"## {title}\n{bar}\n{content}"


# ── Source loaders ────────────────────────────────────────────────────────────

def _load_memory_file(rel: str, budget: int, label: str) -> str:
    """Load a single memory file; return '' if missing/empty."""
    fp = MEMORY_DIR / rel
    if not fp.exists():
        return ""
    try:
        txt = fp.read_text(encoding="utf-8").strip()
        if not txt:
            return ""
        return _trunc(txt, budget, label)
    except Exception as exc:
        _log(f"Could not read {rel}: {exc}")
        return ""


def _load_sessions() -> str:
    """
    Load the most recent session summaries from memory/sessions/.
    Returns a combined string within SESSIONS_BUDGET chars.
    """
    if not SESSIONS_DIR.exists():
        return ""

    # Collect all .md files, sort by filename descending (newest first)
    files = sorted(
        [f for f in SESSIONS_DIR.iterdir() if f.suffix == ".md" and f.is_file()],
        key=lambda f: f.name,
        reverse=True,
    )
    if not files:
        return ""

    parts: list[str] = []
    remaining = SESSIONS_BUDGET
    count = 0

    for fp in files[:MAX_SESSIONS]:
        try:
            txt = fp.read_text(encoding="utf-8").strip()
            if not txt:
                continue
            # Each summary gets an equal share of the remaining budget
            share = remaining // max(1, MAX_SESSIONS - count)
            entry = _trunc(txt, share, f"session {fp.stem}")
            parts.append(entry)
            remaining -= len(entry)
            count += 1
            if remaining <= 0:
                break
        except Exception:
            continue

    if not parts:
        return ""

    combined = "\n\n---\n\n".join(parts)
    return combined


def _load_workspace_context() -> str:
    """
    Auto-scan the workspace root for useful project context files.
    Returns a combined summary within WORKSPACE_BUDGET chars.
    """
    parts: list[str] = []
    remaining = WORKSPACE_BUDGET
    seen_labels: set[str] = set()

    for filename, label in WORKSPACE_SCAN_FILES:
        if label in seen_labels:
            continue  # one per category
        fp = WORKSPACE / filename
        if not fp.exists():
            continue
        try:
            txt = fp.read_text(encoding="utf-8").strip()
            if not txt:
                continue

            # For large files (README, CLAUDE.md), extract the first useful chunk
            chunk = _trunc(txt, min(remaining // 2, 800), label)
            entry = f"**{filename}** ({label}):\n```\n{chunk}\n```"
            parts.append(entry)
            remaining -= len(entry)
            seen_labels.add(label)
            if remaining <= 200:
                break
        except Exception:
            continue

    return "\n\n".join(parts) if parts else ""


# ── Assembly ──────────────────────────────────────────────────────────────────

def _assemble_instructions() -> str:
    """
    Assemble the full instructions string from all memory sources.
    Returns the assembled string (may be empty if all sources are empty).
    """
    blocks: list[str] = []

    # 1. Core memory files
    global_txt  = _load_memory_file("GLOBAL.md",      GLOBAL_BUDGET,   "GLOBAL.md")
    project_txt = _load_memory_file("PROJECT.md",     PROJECT_BUDGET,  "PROJECT.md")
    conv_txt    = _load_memory_file("CONVENTIONS.md", CONV_BUDGET,     "CONVENTIONS.md")
    todo_txt    = _load_memory_file("TODO.md",        TODO_BUDGET,     "TODO.md")

    if global_txt:
        blocks.append(_section("Global Memory  (GLOBAL.md)", global_txt))
        _log(f"  GLOBAL.md    {len(global_txt):,} chars")
    if project_txt:
        blocks.append(_section("Project Memory  (PROJECT.md)", project_txt))
        _log(f"  PROJECT.md   {len(project_txt):,} chars")
    if conv_txt:
        blocks.append(_section("Coding Conventions  (CONVENTIONS.md)", conv_txt))
        _log(f"  CONVENTIONS  {len(conv_txt):,} chars")
    if todo_txt:
        blocks.append(_section("Pending Tasks  (TODO.md)", todo_txt))
        _log(f"  TODO.md      {len(todo_txt):,} chars")

    # 2. Extra memory files (extension point)
    for rel, label, budget in EXTRA_MEMORY_FILES:
        txt = _load_memory_file(rel, budget, rel)
        if txt:
            blocks.append(_section(f"{label}  ({rel})", txt))
            _log(f"  {rel}    {len(txt):,} chars")

    # 3. Recent session summaries
    sessions_txt = _load_sessions()
    if sessions_txt:
        blocks.append(_section("Recent Session Summaries", sessions_txt))
        _log(f"  Sessions     {len(sessions_txt):,} chars")

    # 4. Workspace context (auto-scanned)
    workspace_txt = _load_workspace_context()
    if workspace_txt:
        blocks.append(_section("Workspace Context  (auto-scanned)", workspace_txt))
        _log(f"  Workspace    {len(workspace_txt):,} chars")

    if not blocks:
        # No memory sources found — inject only the maintenance instructions
        # so the AI knows how to bootstrap its own memory.
        _log("No memory files found — injecting bootstrap instructions only")
        return MEMORY_INSTRUCTIONS + (
            "\n\n"
            "─────────────────────────────────────────────────────────\n"
            "NO MEMORY LOADED YET\n"
            "─────────────────────────────────────────────────────────\n"
            "Your memory files don't exist yet. Start building them:\n"
            "  • Create /projects/default/memory/GLOBAL.md\n"
            "  • Create /projects/default/memory/PROJECT.md\n"
            "  • Create /projects/default/memory/CONVENTIONS.md\n"
            "  • Create /projects/default/memory/TODO.md\n"
            "\nUpdate them as you learn about the project and user preferences."
        )

    separator = "\n\n" + ("═" * 60) + "\n\n"
    memory_body = separator.join(blocks)

    # Assemble: instructions header + separator + memory body
    full = (
        MEMORY_INSTRUCTIONS
        + "\n\n"
        + ("═" * 60)
        + "\nYOUR CURRENT MEMORY\n"
        + ("═" * 60)
        + "\n\n"
        + memory_body
    )

    # Final hard cap
    if len(full) > TOTAL_BUDGET:
        full = _trunc(full, TOTAL_BUDGET, "memory context")

    return full


# ── Config writer ─────────────────────────────────────────────────────────────

def _update_config(instructions: str) -> bool:
    """
    Merge `instructions` into opencode.json (preserving all other fields).
    Returns True when the file was actually changed on disk.
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
        return False  # no change — don't write

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(d, indent=2), encoding="utf-8")
    return True


# ── Change detection ──────────────────────────────────────────────────────────

def _state_hash() -> str:
    """
    Cheap fingerprint of all memory sources.
    Changes when any memory file is added, removed, or modified.
    """
    h = hashlib.md5()

    # Core memory files
    for rel in ("GLOBAL.md", "PROJECT.md", "CONVENTIONS.md", "TODO.md"):
        fp = MEMORY_DIR / rel
        if fp.exists():
            try:
                h.update(fp.stat().st_mtime_ns.to_bytes(8, "little"))
                h.update(fp.read_bytes())
            except OSError:
                pass

    # Extra memory files
    for rel, _, _ in EXTRA_MEMORY_FILES:
        fp = MEMORY_DIR / rel
        if fp.exists():
            try:
                h.update(fp.stat().st_mtime_ns.to_bytes(8, "little"))
                h.update(fp.read_bytes())
            except OSError:
                pass

    # Session summaries — just track count and newest mtime
    if SESSIONS_DIR.exists():
        files = sorted(SESSIONS_DIR.glob("*.md"), key=lambda f: f.name, reverse=True)
        for fp in files[:MAX_SESSIONS]:
            try:
                h.update(fp.stat().st_mtime_ns.to_bytes(8, "little"))
                h.update(fp.name.encode())
            except OSError:
                pass

    # Workspace files
    for filename, _ in WORKSPACE_SCAN_FILES:
        fp = WORKSPACE / filename
        if fp.exists():
            try:
                h.update(fp.stat().st_mtime_ns.to_bytes(8, "little"))
            except OSError:
                pass

    return h.hexdigest()


# ── Template initialisation ───────────────────────────────────────────────────

def _init_templates() -> None:
    """
    Create default memory file templates if they don't exist yet.
    Only creates files — never overwrites existing content.
    """
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    templates = {
        "GLOBAL.md": """\
# Global Memory

## User Preferences
<!-- Add coding style preferences, editor settings, workflow habits -->

## Preferred Technologies
<!-- Add preferred languages, frameworks, libraries, tools -->

## Formatting Rules
<!-- Add code formatting preferences, line length, indentation style -->

## Reusable Patterns
<!-- Add patterns, snippets, or approaches to reuse across projects -->

## Communication Style
<!-- How the user likes responses: concise / detailed, with examples / without, etc. -->
""",
        "PROJECT.md": """\
# Project Memory

## Project Overview
<!-- Briefly describe what this project does and its main goals -->

## Architecture
<!-- Describe the high-level architecture, key components, and how they interact -->

## Tech Stack
<!-- List languages, frameworks, databases, external services -->

## APIs & Interfaces
<!-- Document important APIs, endpoints, schemas, or interfaces -->

## Design Decisions
<!-- Record important architectural or design choices and why they were made -->

## Completed Features
<!-- List features that have been implemented -->

## Known Issues
<!-- Record known bugs, limitations, or technical debt -->
""",
        "CONVENTIONS.md": """\
# Coding Conventions

## Naming
<!-- Variable, function, class, file naming rules -->

## Code Style
<!-- Formatting, linting, documentation standards -->

## Patterns & Anti-patterns
<!-- Project-specific patterns to follow or avoid -->

## Testing
<!-- Testing approach, coverage expectations, test naming -->

## Git & Workflow
<!-- Branch naming, commit message format, PR process -->
""",
        "TODO.md": """\
# Pending Tasks

## High Priority
<!-- Critical tasks that need immediate attention -->

## In Progress
<!-- Tasks currently being worked on -->

## Backlog
<!-- Future features, improvements, ideas -->

## Completed (recent)
<!-- Recently completed tasks — remove when no longer relevant -->
""",
    }

    for filename, content in templates.items():
        fp = MEMORY_DIR / filename
        if not fp.exists():
            fp.write_text(content, encoding="utf-8")
            _log(f"Created template: memory/{filename}")


# ── Modes ─────────────────────────────────────────────────────────────────────

def once() -> None:
    """Run a single assembly cycle."""
    _log("Assembling memory context...")
    _init_templates()
    instructions = _assemble_instructions()
    changed = _update_config(instructions)
    total = len(instructions)
    _log(f"Instructions ready: {total:,} chars  (changed={changed})")
    if not changed:
        _log("  (no change — config unchanged)")


def watch() -> None:
    """
    Poll all memory sources for changes every INTERVAL seconds.
    Runs one full assembly immediately, then enters the polling loop.
    """
    _log(f"Memory watcher started — polling every {INTERVAL}s")
    _init_templates()

    last_hash: str = ""
    while True:
        try:
            h = _state_hash()
            if h != last_hash:
                last_hash = h
                _log("Change detected — re-assembling memory context...")
                instructions = _assemble_instructions()
                changed = _update_config(instructions)
                if changed:
                    _log(f"Config updated: {len(instructions):,} chars")
                else:
                    _log("No config change (content identical)")
        except Exception as exc:
            _log(f"Watch cycle error: {exc}")
        time.sleep(INTERVAL)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "once"
    if mode == "watch":
        once()
        watch()
    elif mode == "once":
        once()
    else:
        print("Usage: memory_updater.py [once|watch]", file=sys.stderr)
        sys.exit(1)
