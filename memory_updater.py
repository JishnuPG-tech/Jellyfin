#!/usr/bin/env python3
"""
OpenCode Memory Assembly Engine  (v2)
======================================
Assembles structured persistent memory from multiple typed sources and writes
it to opencode.json's `instructions` field.  OpenCode injects this as a
system-level prompt for every new conversation.

Memory sources (assembled in priority order):
  1. memory/GLOBAL.md        — permanent: user preferences, tech choices
  2. memory/PROJECT.md       — project: architecture, APIs, design decisions
  3. memory/CONVENTIONS.md   — project: coding rules, naming, patterns
  4. memory/TODO.md          — project/temporary: tasks, roadmap
  5. memory/sessions/        — recent conversation summaries (newest first)
  6. Workspace auto-scan     — README, package.json, pyproject.toml, etc.

Importance levels (tag your sections in memory files):
  [PERMANENT]  — core facts that never expire (coding style, project goals)
  [PROJECT]    — project-specific knowledge (APIs, folder structure, decisions)
  [TEMPORARY]  — time-limited info (active debugging, today's task)
                 Add <!-- expires: YYYY-MM-DD --> inside to auto-expire.
  (no tag)     — treated as [PROJECT]

Semantic retrieval interface:
  MemoryRetriever is the abstract base.  KeywordRetriever is the default.
  To add embeddings/vector search later, subclass MemoryRetriever and set
  ACTIVE_RETRIEVER at the bottom of this file — no other changes needed.

Usage:
  python3 /memory_updater.py once    — assemble once then exit (startup)
  python3 /memory_updater.py watch   — poll every INTERVAL secs for changes
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

# ── Paths ─────────────────────────────────────────────────────────────────────
WORKSPACE      = Path("/projects/default")
MEMORY_DIR     = WORKSPACE / "memory"
SESSIONS_DIR   = MEMORY_DIR / "sessions"
CONFIG_PATH    = Path("/data/config/opencode/opencode.json")
ASSEMBLED_PATH = MEMORY_DIR / "ASSEMBLED.md"   # instructions written here; path injected into config

# ── Character budgets ─────────────────────────────────────────────────────────
TOTAL_BUDGET        = 10_000   # hard cap for the whole instructions field
GLOBAL_BUDGET       = 1_800
PROJECT_BUDGET      = 2_800
CONV_BUDGET         = 1_200
TODO_BUDGET         = 1_000
SESSIONS_BUDGET     = 1_800
WORKSPACE_BUDGET    = 1_200
MAX_SESSIONS        = 5        # newest N summaries to include

# ── Polling ───────────────────────────────────────────────────────────────────
INTERVAL = 15   # seconds

# ── Workspace files to auto-scan ──────────────────────────────────────────────
WORKSPACE_SCAN_FILES: list[tuple[str, str]] = [
    ("README.md",        "Project README"),
    ("README.rst",       "Project README"),
    ("README.txt",       "Project README"),
    ("package.json",     "Node.js Manifest"),
    ("pyproject.toml",   "Python Project"),
    ("requirements.txt", "Python Dependencies"),
    ("Cargo.toml",       "Rust Manifest"),
    ("go.mod",           "Go Module"),
    ("composer.json",    "PHP Manifest"),
    ("pom.xml",          "Maven Project"),
    ("build.gradle",     "Gradle Project"),
    ("CLAUDE.md",        "Project Instructions"),
    ("AGENTS.md",        "Agent Instructions"),
    (".cursorrules",     "Cursor Rules"),
]

# ── Extension point ───────────────────────────────────────────────────────────
# Add entries here to include additional memory files.
# Each: (relative path under MEMORY_DIR, section label, char budget)
EXTRA_MEMORY_FILES: list[tuple[str, str, int]] = [
    # ("TEAM.md",   "Team Guidelines",  800),
    # ("RULES.md",  "Additional Rules", 800),
]

# ── Importance level ordering ─────────────────────────────────────────────────
IMPORTANCE_ORDER = {"permanent": 0, "project": 1, "temporary": 2}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [MEMORY] {msg}", flush=True)


def _trunc(text: str, budget: int, label: str = "") -> str:
    if len(text) <= budget:
        return text
    cut = text[:budget]
    nl = cut.rfind("\n")
    if nl > budget * 0.7:
        cut = cut[:nl]
    note = f"\n\n[... {label} truncated ...]" if label else "\n\n[... truncated ...]"
    return cut + note


def _is_placeholder(text: str) -> bool:
    """
    Return True if a section contains only template placeholder comments
    (the default templates use <!-- ... --> blocks as hints to the user).
    """
    stripped = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL).strip()
    return len(stripped) < 10


# ── MemorySection dataclass ───────────────────────────────────────────────────

_IMPORTANCE_RE = re.compile(r"\[(PERMANENT|PROJECT|TEMPORARY)\]", re.IGNORECASE)
_EXPIRES_RE    = re.compile(r"<!--\s*expires:\s*(\d{4}-\d{2}-\d{2})\s*-->", re.IGNORECASE)


@dataclass
class MemorySection:
    """
    A single ## section parsed from a memory markdown file.

    importance: "permanent" | "project" | "temporary"
    expires:    date or None (only meaningful for temporary sections)
    """
    source:      str
    heading:     str          # cleaned (tag stripped)
    raw_heading: str          # original
    importance:  str          # permanent / project / temporary
    expires:     Optional[date]
    content:     str          # section body text

    @property
    def is_expired(self) -> bool:
        if self.importance != "temporary" or self.expires is None:
            return False
        return date.today() > self.expires

    @property
    def priority(self) -> int:
        return IMPORTANCE_ORDER.get(self.importance, 1)

    @property
    def char_len(self) -> int:
        return len(self.heading) + len(self.content) + 10  # approx rendered size

    def render(self, show_importance: bool = True) -> str:
        tag = f"[{self.importance.upper()}] " if show_importance else ""
        header = f"### {tag}{self.heading}"
        expiry_note = ""
        if self.importance == "temporary" and self.expires:
            expiry_note = f"\n*Expires: {self.expires}*"
        return f"{header}{expiry_note}\n{self.content}"


# ── Section parser ────────────────────────────────────────────────────────────

def parse_sections(content: str, source: str) -> list[MemorySection]:
    """
    Split a memory markdown file into typed MemorySection objects.
    Sections are delimited by ## headings.
    Lines before the first ## heading are ignored (file-level title).
    """
    sections: list[MemorySection] = []
    current_raw_heading: Optional[str] = None
    current_lines: list[str] = []

    def _flush():
        if current_raw_heading is None:
            return
        body = "\n".join(current_lines).strip()
        if not body or _is_placeholder(body):
            return

        m_imp = _IMPORTANCE_RE.search(current_raw_heading)
        importance = m_imp.group(1).lower() if m_imp else "project"
        heading = _IMPORTANCE_RE.sub("", current_raw_heading).strip()

        expires: Optional[date] = None
        m_exp = _EXPIRES_RE.search(body)
        if m_exp:
            try:
                expires = date.fromisoformat(m_exp.group(1))
            except ValueError:
                pass

        sections.append(MemorySection(
            source=source,
            heading=heading,
            raw_heading=current_raw_heading,
            importance=importance,
            expires=expires,
            content=body,
        ))

    for line in content.splitlines():
        if line.startswith("## "):
            _flush()
            current_raw_heading = line[3:]
            current_lines = []
        elif current_raw_heading is not None:
            current_lines.append(line)

    _flush()
    return sections


# ── MemoryRetriever interface ─────────────────────────────────────────────────
#
# Designed to be swapped with a SemanticRetriever without changing anything
# else.  The assembly engine calls ACTIVE_RETRIEVER.rank() and passes the
# result through ACTIVE_RETRIEVER.filter_expired().
#
# To add semantic search:
#   1. Subclass MemoryRetriever.
#   2. Override rank() to embed the query and sort by cosine similarity.
#   3. Set ACTIVE_RETRIEVER = MySemanticRetriever() at the bottom of this file.
#
class MemoryRetriever:
    """
    Abstract interface for memory section retrieval.

    rank(sections, query) → ordered list (most relevant first)
    filter_expired(sections) → sections with is_expired=False

    Default (KeywordRetriever): importance-based order, no semantic ranking.
    Future: SemanticRetriever can rank by embedding similarity to the query.
    """

    def rank(
        self,
        sections: list[MemorySection],
        query: Optional[str] = None,
    ) -> list[MemorySection]:
        raise NotImplementedError

    def filter_expired(self, sections: list[MemorySection]) -> list[MemorySection]:
        return [s for s in sections if not s.is_expired]


class KeywordRetriever(MemoryRetriever):
    """
    Default retriever: sorts by importance level, then source, then heading.
    Optionally boosts sections whose heading or content contain query keywords.
    Does NOT require any external library.
    """

    def rank(
        self,
        sections: list[MemorySection],
        query: Optional[str] = None,
    ) -> list[MemorySection]:
        if not query:
            return sorted(sections, key=lambda s: (s.priority, s.source, s.heading))

        query_words = set(re.findall(r"\w+", query.lower()))

        def _score(s: MemorySection) -> tuple:
            text = (s.heading + " " + s.content).lower()
            words = set(re.findall(r"\w+", text))
            overlap = len(query_words & words)
            # Primary: importance; secondary: keyword overlap (higher = better); tertiary: heading
            return (s.priority, -overlap, s.heading)

        return sorted(sections, key=_score)


# Set the active retriever here.  Swap to a semantic implementation later.
ACTIVE_RETRIEVER: MemoryRetriever = KeywordRetriever()


# ── Workspace fingerprint cache ───────────────────────────────────────────────

class WorkspaceCache:
    """
    Tracks file mtimes so workspace files are only re-read when they change.
    Avoids re-scanning unchanged README/package.json on every 15-second poll.
    """

    def __init__(self) -> None:
        self._mtimes: dict[Path, float] = {}
        self._content: dict[Path, str]  = {}

    def read(self, path: Path) -> Optional[str]:
        """Return cached content, refreshing only when the file has changed."""
        try:
            mtime = path.stat().st_mtime
        except OSError:
            # File disappeared
            self._mtimes.pop(path, None)
            self._content.pop(path, None)
            return None

        if self._mtimes.get(path) != mtime:
            try:
                self._content[path] = path.read_text(encoding="utf-8").strip()
                self._mtimes[path]  = mtime
            except OSError:
                return None

        return self._content.get(path) or None


_workspace_cache = WorkspaceCache()


# ── Source loaders ────────────────────────────────────────────────────────────

def _load_memory_sections(filename: str, budget: int) -> list[MemorySection]:
    """Load and parse one memory file into typed sections."""
    fp = MEMORY_DIR / filename
    if not fp.exists():
        return []
    try:
        raw = fp.read_text(encoding="utf-8").strip()
        if not raw:
            return []
        return parse_sections(raw, filename)
    except Exception as exc:
        _log(f"Could not read {filename}: {exc}")
        return []


def _sections_to_text(sections: list[MemorySection], budget: int, label: str) -> str:
    """
    Render a list of MemorySection objects to a string within `budget` chars.
    Permanent sections are always included first; temporary last.
    Sections that would exceed the budget are dropped (not truncated mid-way).
    """
    ordered = ACTIVE_RETRIEVER.rank(
        ACTIVE_RETRIEVER.filter_expired(sections)
    )
    parts: list[str] = []
    used = 0
    for section in ordered:
        rendered = section.render()
        if used + len(rendered) > budget:
            if not parts:
                # Force-include at least one section, truncated
                parts.append(_trunc(rendered, budget, label))
            break
        parts.append(rendered)
        used += len(rendered)

    return "\n\n".join(parts)


def _load_sessions() -> str:
    """Load the most recent session summaries (newest first)."""
    if not SESSIONS_DIR.exists():
        return ""

    # Exclude archive files
    files = sorted(
        [f for f in SESSIONS_DIR.iterdir()
         if f.suffix == ".md" and f.is_file() and not f.name.startswith("ARCHIVE_")],
        key=lambda f: f.name,
        reverse=True,
    )
    if not files:
        return ""

    parts: list[str] = []
    remaining = SESSIONS_BUDGET

    for fp in files[:MAX_SESSIONS]:
        try:
            txt = (fp.read_text(encoding="utf-8") or "").strip()
            if not txt:
                continue
            share = remaining // max(1, MAX_SESSIONS - len(parts))
            entry = _trunc(txt, share, f"session {fp.stem}")
            parts.append(entry)
            remaining -= len(entry)
            if remaining <= 0:
                break
        except Exception:
            continue

    return "\n\n---\n\n".join(parts) if parts else ""


def _load_workspace_context() -> str:
    """
    Auto-scan the workspace for project context files.
    Uses WorkspaceCache so unchanged files are not re-read on every poll.
    """
    parts: list[str] = []
    remaining = WORKSPACE_BUDGET
    seen_labels: set[str] = set()

    for filename, label in WORKSPACE_SCAN_FILES:
        if label in seen_labels:
            continue
        fp = WORKSPACE / filename
        txt = _workspace_cache.read(fp)
        if not txt:
            continue
        chunk = _trunc(txt, min(remaining // 2, 800), label)
        entry = f"**{filename}** ({label}):\n```\n{chunk}\n```"
        parts.append(entry)
        remaining -= len(entry)
        seen_labels.add(label)
        if remaining <= 200:
            break

    return "\n\n".join(parts) if parts else ""


# ── System prompt preamble ────────────────────────────────────────────────────

MEMORY_INSTRUCTIONS = """\
You are an AI coding assistant with a persistent memory system. Your memory \
is loaded automatically at the start of every conversation. You already know \
the project, previous decisions, and user preferences — do not ask the user \
to re-explain them.

MEMORY FILES  (/projects/default/memory/)
──────────────────────────────────────────
• GLOBAL.md      → [PERMANENT] user preferences, coding style, tech stack
• PROJECT.md     → [PROJECT]   architecture, APIs, schemas, design decisions
• CONVENTIONS.md → [PROJECT]   naming, formatting, patterns
• TODO.md        → [PROJECT]/[TEMPORARY]  tasks, roadmap

IMPORTANCE LEVELS  (tag each section header)
─────────────────────────────────────────────
  ## [PERMANENT] Preferred Code Style     ← never expires
  ## [PROJECT]   Authentication Flow      ← project-scoped, long-lived
  ## [TEMPORARY] Current Debugging Task   ← add expiry:
  <!-- expires: YYYY-MM-DD -->

Expired [TEMPORARY] sections are automatically excluded from context.

WHEN TO UPDATE YOUR MEMORY
────────────────────────────
After any meaningful conversation, update the appropriate file:
  ✓ Architecture or design decisions     → PROJECT.md
  ✓ User preferences discovered          → GLOBAL.md   [PERMANENT]
  ✓ New coding conventions learned       → CONVENTIONS.md
  ✓ New tasks or completed work          → TODO.md
  ✓ Active debugging / today's task      → TODO.md     [TEMPORARY] + expiry
  ✗ Temporary debugging, casual chat, one-off questions — do NOT save

Use your file-editing tools to update the file directly.
Keep sections concise. Replace outdated facts; add new ones.

SESSION SUMMARIES
──────────────────
After any meaningful work session, write a summary to:
  /projects/default/memory/sessions/YYYY-MM-DD_HH-MM.md

Format:
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

MEMORY MANAGEMENT
──────────────────
From the terminal: run `memctl` for memory inspection and editing tools.
"""


# ── Assembly ──────────────────────────────────────────────────────────────────

def _section_block(title: str, content: str) -> str:
    bar = "─" * min(len(title) + 4, 60)
    return f"## {title}\n{bar}\n{content}"


def _assemble_instructions() -> str:
    blocks: list[str] = []
    total_logged = 0

    # 1. Core memory files
    for filename, label, budget in [
        ("GLOBAL.md",      "Global Memory",        GLOBAL_BUDGET),
        ("PROJECT.md",     "Project Memory",        PROJECT_BUDGET),
        ("CONVENTIONS.md", "Coding Conventions",    CONV_BUDGET),
        ("TODO.md",        "Pending Tasks & Notes", TODO_BUDGET),
    ]:
        sections = _load_memory_sections(filename, budget)
        if sections:
            txt = _sections_to_text(sections, budget, filename)
            if txt:
                blocks.append(_section_block(f"{label}  ({filename})", txt))
                total_logged += len(txt)
                _log(f"  {filename:<16} {len(txt):>6,} chars  "
                     f"({len(sections)} sections, "
                     f"{sum(1 for s in sections if s.importance == 'permanent')} perm, "
                     f"{sum(1 for s in sections if s.importance == 'temporary')} temp)")

    # 2. Extra memory files (extension point)
    for rel, label, budget in EXTRA_MEMORY_FILES:
        sections = _load_memory_sections(rel, budget)
        if sections:
            txt = _sections_to_text(sections, budget, rel)
            if txt:
                blocks.append(_section_block(f"{label}  ({rel})", txt))
                total_logged += len(txt)
                _log(f"  {rel:<16} {len(txt):>6,} chars")

    # 3. Recent session summaries
    sessions_txt = _load_sessions()
    if sessions_txt:
        blocks.append(_section_block("Recent Session Summaries", sessions_txt))
        total_logged += len(sessions_txt)
        _log(f"  {'sessions':<16} {len(sessions_txt):>6,} chars")

    # 4. Workspace context (mtime-cached)
    workspace_txt = _load_workspace_context()
    if workspace_txt:
        blocks.append(_section_block("Workspace Context  (auto-scanned)", workspace_txt))
        total_logged += len(workspace_txt)
        _log(f"  {'workspace':<16} {len(workspace_txt):>6,} chars")

    if not blocks:
        _log("No memory content found — injecting bootstrap instructions only")
        return MEMORY_INSTRUCTIONS + (
            "\n\n"
            + ("─" * 60) + "\n"
            "NO MEMORY LOADED YET\n"
            + ("─" * 60) + "\n\n"
            "Memory files are empty or contain only placeholder comments.\n"
            "Start filling them in:\n"
            "  • /projects/default/memory/GLOBAL.md\n"
            "  • /projects/default/memory/PROJECT.md\n"
            "  • /projects/default/memory/CONVENTIONS.md\n"
            "  • /projects/default/memory/TODO.md\n\n"
            "Or run `memctl status` in the terminal to inspect the memory system."
        )

    sep = "\n\n" + ("═" * 60) + "\n\n"
    memory_body = sep.join(blocks)

    full = (
        MEMORY_INSTRUCTIONS
        + "\n\n"
        + ("═" * 60) + "\n"
        + "YOUR CURRENT MEMORY\n"
        + ("═" * 60) + "\n\n"
        + memory_body
    )

    if len(full) > TOTAL_BUDGET:
        full = _trunc(full, TOTAL_BUDGET, "memory context (total budget)")

    return full


# ── Config writer ─────────────────────────────────────────────────────────────

def _validate_config(d: dict) -> tuple[bool, str]:
    """
    Lightweight schema check that mirrors OpenCode's Zod validation rules.
    OpenCode 1.18.3+: instructions must be array[string] | undefined.
    A memory update must NEVER produce an invalid config.
    """
    instr = d.get("instructions")
    if instr is None:
        pass  # optional → OK
    elif not isinstance(instr, list):
        return False, (
            f"instructions must be array or absent, got {type(instr).__name__!r} "
            f"(value={str(instr)[:80]!r})"
        )
    else:
        for i, item in enumerate(instr):
            if not isinstance(item, str):
                return False, (
                    f"instructions[{i}] must be str, got {type(item).__name__!r}"
                )
    server = d.get("server")
    if server is not None and not isinstance(server, dict):
        return False, f"server must be object, got {type(server).__name__!r}"
    return True, "OK"


def _update_config(instructions: str) -> bool:
    """
    Production-safe config-update pipeline:

      1. Write assembled instructions text → ASSEMBLED.md (atomic rename)
      2. Read existing opencode.json        (preserve ALL existing settings)
      3. Set instructions = [str(ASSEMBLED_PATH)]
         (array of file paths — OpenCode reads each file as system prompt)
      4. Validate the new config against the known schema
      5a. Valid   → atomic write to opencode.json
      5b. Invalid → restore backup, log error, return False

    A memory update must NEVER prevent OpenCode from starting.
    If anything fails, the previous working config is preserved.
    """
    # ── 1. Write assembled instructions to the markdown file (atomic) ──
    ASSEMBLED_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_md = ASSEMBLED_PATH.with_suffix(".tmp")
    try:
        tmp_md.write_text(instructions, encoding="utf-8")
        tmp_md.replace(ASSEMBLED_PATH)   # atomic on POSIX (same fs)
        _log(f"  ASSEMBLED.md  {len(instructions):,} chars written")
    except Exception as exc:
        _log(f"[ERROR] Could not write {ASSEMBLED_PATH}: {exc}")
        return False

    # ── 2. Read existing config (preserve all keys) ────────────────────
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing_text = (
            CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else "{}"
        )
        d: dict = json.loads(existing_text)
    except Exception as exc:
        _log(f"[WARN] Unreadable existing config ({exc}) — starting fresh")
        existing_text = "{}"
        d = {}

    # ── 3. Backup existing config ──────────────────────────────────────
    backup_path = CONFIG_PATH.with_suffix(".json.bak")
    try:
        backup_path.write_text(existing_text, encoding="utf-8")
    except Exception as exc:
        _log(f"[WARN] Backup failed: {exc}")

    # ── 4. Inject instructions file-path reference ─────────────────────
    new_instr: list[str] = [str(ASSEMBLED_PATH)]
    if d.get("instructions") == new_instr:
        # Pointer unchanged; ASSEMBLED.md content updated above — done.
        _log("instructions pointer unchanged — config rewrite skipped")
        return False
    d["instructions"] = new_instr

    # ── 5. Validate ────────────────────────────────────────────────────
    ok, reason = _validate_config(d)
    if not ok:
        _log(f"[ERROR] Validation failed: {reason}")
        _log("[ROLLBACK] Restoring previous config from backup…")
        try:
            CONFIG_PATH.write_text(existing_text, encoding="utf-8")
            _log("[ROLLBACK] Previous config restored — OpenCode will start safely")
        except Exception as re_exc:
            _log(f"[ROLLBACK ERROR] Could not restore: {re_exc}")
        return False

    # ── 6. Atomic write ────────────────────────────────────────────────
    tmp_cfg = CONFIG_PATH.with_suffix(".json.tmp")
    new_text = json.dumps(d, indent=2)
    try:
        tmp_cfg.write_text(new_text, encoding="utf-8")
        tmp_cfg.replace(CONFIG_PATH)     # atomic rename
        _log(f'[OK] Config updated — instructions → ["{ASSEMBLED_PATH.name}"]')
        return True
    except Exception as exc:
        _log(f"[ERROR] Atomic write failed: {exc}")
        _log("[ROLLBACK] Restoring previous config…")
        try:
            CONFIG_PATH.write_text(existing_text, encoding="utf-8")
            _log("[ROLLBACK] Previous config restored")
        except Exception as re_exc:
            _log(f"[ROLLBACK ERROR] Could not restore: {re_exc}")
        return False


# ── Change fingerprint ────────────────────────────────────────────────────────

def _state_hash() -> str:
    """
    Cheap fingerprint over all memory sources.
    Uses mtime for workspace files (no re-read needed) and content hash for
    memory files (they're small and content matters more than mtime).
    """
    h = hashlib.md5()

    # Core memory files — hash content
    for fn in ("GLOBAL.md", "PROJECT.md", "CONVENTIONS.md", "TODO.md"):
        fp = MEMORY_DIR / fn
        if fp.exists():
            try:
                h.update(fp.stat().st_mtime_ns.to_bytes(8, "little"))
                h.update(fp.read_bytes())
            except OSError:
                pass

    # Extra files
    for rel, _, _ in EXTRA_MEMORY_FILES:
        fp = MEMORY_DIR / rel
        if fp.exists():
            try:
                h.update(fp.stat().st_mtime_ns.to_bytes(8, "little"))
                h.update(fp.read_bytes())
            except OSError:
                pass

    # Sessions — track count + newest mtime (not content)
    if SESSIONS_DIR.exists():
        files = sorted(SESSIONS_DIR.glob("*.md"), key=lambda f: f.name, reverse=True)
        for fp in files[:MAX_SESSIONS + 1]:
            try:
                h.update(fp.name.encode())
                h.update(fp.stat().st_mtime_ns.to_bytes(8, "little"))
            except OSError:
                pass

    # Workspace files — mtime only (WorkspaceCache handles the re-read)
    for fn, _ in WORKSPACE_SCAN_FILES:
        fp = WORKSPACE / fn
        if fp.exists():
            try:
                h.update(fp.stat().st_mtime_ns.to_bytes(8, "little"))
            except OSError:
                pass

    return h.hexdigest()


# ── Template initialisation ───────────────────────────────────────────────────

def _init_templates() -> None:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    templates = {
        "GLOBAL.md": """\
# Global Memory

## [PERMANENT] Coding Preferences
<!-- Add coding style preferences, language choices, editor settings -->

## [PERMANENT] Preferred Technologies
<!-- Add preferred languages, frameworks, libraries, tools -->

## [PERMANENT] Formatting Rules
<!-- Add code formatting preferences, line length, indentation style -->

## [PROJECT] Reusable Patterns
<!-- Add patterns, snippets, or approaches to reuse across this project -->

## [PERMANENT] Communication Style
<!-- How the user likes responses: concise / detailed, with examples / without, etc. -->
""",
        "PROJECT.md": """\
# Project Memory

## [PERMANENT] Project Overview
<!-- Briefly describe what this project does and its main goals -->

## [PROJECT] Architecture
<!-- Describe the high-level architecture, key components, and how they interact -->

## [PROJECT] Tech Stack
<!-- List languages, frameworks, databases, external services -->

## [PROJECT] APIs & Interfaces
<!-- Document important APIs, endpoints, schemas, or interfaces -->

## [PROJECT] Design Decisions
<!-- Record important architectural or design choices and why they were made -->

## [PROJECT] Completed Features
<!-- List features that have been implemented -->

## [PROJECT] Known Issues
<!-- Record known bugs, limitations, or technical debt -->
""",
        "CONVENTIONS.md": """\
# Coding Conventions

## [PERMANENT] Naming
<!-- Variable, function, class, file naming rules -->

## [PERMANENT] Code Style
<!-- Formatting, linting, documentation standards -->

## [PROJECT] Patterns & Anti-patterns
<!-- Project-specific patterns to follow or avoid -->

## [PROJECT] Testing
<!-- Testing approach, coverage expectations, test naming -->

## [PROJECT] Git & Workflow
<!-- Branch naming, commit message format, PR process -->
""",
        "TODO.md": """\
# Pending Tasks

## [PROJECT] High Priority
<!-- Critical tasks that need immediate attention -->

## [TEMPORARY] In Progress
<!-- Tasks currently being worked on — add <!-- expires: YYYY-MM-DD --> -->

## [PROJECT] Backlog
<!-- Future features, improvements, ideas -->

## [PROJECT] Completed (recent)
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
    _log("Assembling memory context...")
    _init_templates()
    instructions = _assemble_instructions()
    changed = _update_config(instructions)
    _log(f"Instructions ready: {len(instructions):,} chars  (changed={changed})")


def watch() -> None:
    _log(f"Memory watcher started — polling every {INTERVAL}s")
    _init_templates()
    last_hash: str = ""
    while True:
        try:
            h = _state_hash()
            if h != last_hash:
                last_hash = h
                _log("Change detected — re-assembling...")
                instructions = _assemble_instructions()
                changed = _update_config(instructions)
                if changed:
                    _log(f"Config updated: {len(instructions):,} chars")
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
