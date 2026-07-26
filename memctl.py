#!/usr/bin/env python3
"""
memctl — OpenCode Memory Management CLI
========================================
Inspect, edit, and maintain the persistent memory system from the terminal.

Usage:
  memctl                        show this help
  memctl status                 memory sources, sizes, section counts
  memctl show global            print GLOBAL.md
  memctl show project           print PROJECT.md
  memctl show conventions       print CONVENTIONS.md
  memctl show todo              print TODO.md
  memctl show sessions [N]      list last N session summaries (default 10)
  memctl show session <id>      print a specific session summary by ID prefix
  memctl edit global            open GLOBAL.md in $EDITOR (fallback: nano)
  memctl edit project           open PROJECT.md
  memctl edit conventions       open CONVENTIONS.md
  memctl edit todo              open TODO.md
  memctl clear expired          remove expired [TEMPORARY] sections from all files
  memctl clear sessions [--yes] delete ALL session summaries (prompts for confirm)
  memctl archive                force-archive old sessions now
  memctl rebuild                re-assemble memory context → opencode.json now
  memctl inspect <file>         parse sections and show their importance/expiry
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

# ── Paths ─────────────────────────────────────────────────────────────────────
WORKSPACE    = Path("/projects/default")
MEMORY_DIR   = WORKSPACE / "memory"
SESSIONS_DIR = MEMORY_DIR / "sessions"
CONFIG_PATH  = Path("/data/config/opencode/opencode.json")

FILE_ALIASES = {
    "global":      MEMORY_DIR / "GLOBAL.md",
    "project":     MEMORY_DIR / "PROJECT.md",
    "conventions": MEMORY_DIR / "CONVENTIONS.md",
    "conv":        MEMORY_DIR / "CONVENTIONS.md",
    "todo":        MEMORY_DIR / "TODO.md",
}

ARCHIVE_THRESH = int(os.environ.get("SESSION_ARCHIVE_N", "25"))
ARCHIVE_KEEP   = int(os.environ.get("SESSION_KEEP_N", "10"))


# ── ANSI colours ──────────────────────────────────────────────────────────────

NO_COLOR = not sys.stdout.isatty() or os.environ.get("NO_COLOR")


def _c(text: str, code: str) -> str:
    return text if NO_COLOR else f"\033[{code}m{text}\033[0m"


def bold(t: str)    -> str: return _c(t, "1")
def dim(t: str)     -> str: return _c(t, "2")
def green(t: str)   -> str: return _c(t, "32")
def yellow(t: str)  -> str: return _c(t, "33")
def red(t: str)     -> str: return _c(t, "31")
def cyan(t: str)    -> str: return _c(t, "36")
def magenta(t: str) -> str: return _c(t, "35")
def blue(t: str)    -> str: return _c(t, "34")


# ── Importance parsing ────────────────────────────────────────────────────────

_IMPORTANCE_RE = re.compile(r"\[(PERMANENT|PROJECT|TEMPORARY)\]", re.IGNORECASE)
_EXPIRES_RE    = re.compile(r"<!--\s*expires:\s*(\d{4}-\d{2}-\d{2})\s*-->", re.IGNORECASE)
_PLACEHOLDER_RE = re.compile(r"^<!--.*?-->$", re.DOTALL | re.MULTILINE)


@dataclass
class Section:
    raw_heading:  str
    heading:      str
    importance:   str
    expires:      Optional[date]
    content:      str
    line_start:   int

    @property
    def is_expired(self) -> bool:
        return self.importance == "temporary" and self.expires is not None and date.today() > self.expires

    @property
    def is_placeholder(self) -> bool:
        stripped = _PLACEHOLDER_RE.sub("", self.content).strip()
        return len(stripped) < 10


def _parse_sections(text: str) -> list[Section]:
    sections: list[Section] = []
    lines = text.splitlines()
    current_raw: Optional[str] = None
    current_lines: list[str] = []
    current_lineno = 0

    def flush():
        if current_raw is None:
            return
        body = "\n".join(current_lines).strip()
        m_imp = _IMPORTANCE_RE.search(current_raw)
        importance = m_imp.group(1).lower() if m_imp else "project"
        heading = _IMPORTANCE_RE.sub("", current_raw).strip()
        expires: Optional[date] = None
        m_exp = _EXPIRES_RE.search(body)
        if m_exp:
            try:
                expires = date.fromisoformat(m_exp.group(1))
            except ValueError:
                pass
        sections.append(Section(
            raw_heading=current_raw,
            heading=heading,
            importance=importance,
            expires=expires,
            content=body,
            line_start=current_lineno,
        ))

    for i, line in enumerate(lines):
        if line.startswith("## "):
            flush()
            current_raw = line[3:]
            current_lines = []
            current_lineno = i + 1
        elif current_raw is not None:
            current_lines.append(line)

    flush()
    return sections


# ── Helpers ───────────────────────────────────────────────────────────────────

def _importance_badge(importance: str) -> str:
    badges = {
        "permanent": green("[PERMANENT]"),
        "project":   cyan("[PROJECT]"),
        "temporary": yellow("[TEMPORARY]"),
    }
    return badges.get(importance, dim("[?]"))


def _file_summary(fp: Path) -> str:
    if not fp.exists():
        return dim("(not found)")
    try:
        txt = fp.read_text(encoding="utf-8")
        sections = _parse_sections(txt)
        real = [s for s in sections if not s.is_placeholder]
        expired = sum(1 for s in real if s.is_expired)
        exp_note = f"  {red(f'{expired} expired')}" if expired else ""
        return (f"{len(txt):>7,} chars  "
                f"{len(real)} section(s){exp_note}")
    except Exception as exc:
        return red(f"(error: {exc})")


def _count_sessions() -> tuple[int, int]:
    """Return (summary_count, archive_count)."""
    if not SESSIONS_DIR.exists():
        return 0, 0
    files = list(SESSIONS_DIR.glob("*.md"))
    archives = [f for f in files if f.name.startswith("ARCHIVE_")]
    summaries = [f for f in files if not f.name.startswith("ARCHIVE_")]
    return len(summaries), len(archives)


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_status() -> None:
    print(bold("\n  OpenCode Memory System — Status\n"))
    bar = "─" * 52

    print(f"  {bold('Memory files')}  ({MEMORY_DIR})")
    print(f"  {bar}")
    rows = [
        ("GLOBAL.md",      "global"),
        ("PROJECT.md",     "project"),
        ("CONVENTIONS.md", "conventions"),
        ("TODO.md",        "todo"),
    ]
    for filename, alias in rows:
        fp = MEMORY_DIR / filename
        badge = green("✓") if fp.exists() else red("✗")
        print(f"  {badge} {filename:<18} {_file_summary(fp)}  {dim(f'(memctl show {alias})')}")

    print()
    print(f"  {bold('Session summaries')}  ({SESSIONS_DIR})")
    print(f"  {bar}")
    n_sum, n_arc = _count_sessions()
    arch_note = f"  {dim(f'+ {n_arc} archive file(s)')}" if n_arc else ""
    print(f"  {green('✓') if n_sum else dim('○')} {n_sum} summary file(s){arch_note}")
    print(f"    Archive threshold: {ARCHIVE_THRESH}  •  Keep recent: {ARCHIVE_KEEP}")
    if n_sum > 0 and SESSIONS_DIR.exists():
        files = sorted(
            [f for f in SESSIONS_DIR.glob("*.md") if not f.name.startswith("ARCHIVE_")],
            key=lambda f: f.name, reverse=True,
        )
        for fp in files[:3]:
            size = fp.stat().st_size
            print(f"    {dim('•')} {fp.name}  {dim(f'{size:,} bytes')}")
        if n_sum > 3:
            print(f"    {dim(f'... and {n_sum - 3} more')}")

    print()
    print(f"  {bold('opencode.json instructions')}")
    print(f"  {bar}")
    if CONFIG_PATH.exists():
        try:
            d = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            inst = d.get("instructions", "")
            if inst:
                print(f"  {green('✓')} {len(inst):,} chars  "
                      f"{dim('(injected as system prompt for every session)')}")
            else:
                print(f"  {yellow('○')} instructions field is empty")
        except Exception:
            print(f"  {red('✗')} could not read config")
    else:
        print(f"  {red('✗')} opencode.json not found")

    print()
    print(f"  {dim('Tip: run')} {bold('memctl rebuild')} {dim('to force re-assembly now.')}")
    print()


def cmd_show(args: list[str]) -> None:
    if not args:
        print(red("Usage: memctl show <global|project|conventions|todo|sessions [N]|session <id>>"))
        sys.exit(1)

    target = args[0].lower()

    if target == "sessions":
        n = int(args[1]) if len(args) > 1 else 10
        _show_sessions(n)
        return

    if target == "session":
        if len(args) < 2:
            print(red("Usage: memctl show session <id-prefix>"))
            sys.exit(1)
        _show_session(args[1])
        return

    fp = FILE_ALIASES.get(target)
    if fp is None:
        print(red(f"Unknown target: {target!r}"))
        print(f"  Valid: {', '.join(FILE_ALIASES)}, sessions, session <id>")
        sys.exit(1)

    if not fp.exists():
        print(yellow(f"File not found: {fp}"))
        print(f"  Run {bold('memctl rebuild')} to initialise memory files.")
        return

    content = fp.read_text(encoding="utf-8")
    print(bold(f"\n  {fp.name}\n  {'─' * 50}"))
    print(content)


def _show_sessions(n: int) -> None:
    if not SESSIONS_DIR.exists() or not any(SESSIONS_DIR.iterdir()):
        print(yellow("  No session summaries found."))
        return
    files = sorted(
        [f for f in SESSIONS_DIR.glob("*.md") if not f.name.startswith("ARCHIVE_")],
        key=lambda f: f.name, reverse=True,
    )[:n]
    print(bold(f"\n  Last {len(files)} session summary file(s):"))
    for fp in files:
        size = fp.stat().st_size
        print(f"\n  {cyan(fp.name)}  {dim(f'{size:,} bytes')}")
        print("  " + "─" * 50)
        try:
            lines = fp.read_text(encoding="utf-8").splitlines()
            preview = "\n  ".join(lines[:20])
            print(f"  {preview}")
            if len(lines) > 20:
                print(f"  {dim(f'... ({len(lines) - 20} more lines — memctl show session {fp.stem[:8]})')}")
        except Exception:
            print(red("  (could not read)"))
    print()


def _show_session(id_prefix: str) -> None:
    if not SESSIONS_DIR.exists():
        print(yellow("  No session summaries found."))
        return
    matches = [f for f in SESSIONS_DIR.glob("*.md") if id_prefix.lower() in f.name.lower()]
    if not matches:
        print(red(f"  No session matching '{id_prefix}'"))
        return
    fp = sorted(matches)[-1]
    print(bold(f"\n  {fp.name}\n  {'─' * 50}"))
    print(fp.read_text(encoding="utf-8"))


def cmd_edit(args: list[str]) -> None:
    if not args:
        print(red("Usage: memctl edit <global|project|conventions|todo>"))
        sys.exit(1)

    target = args[0].lower()
    fp = FILE_ALIASES.get(target)
    if fp is None:
        print(red(f"Unknown target: {target!r}"))
        sys.exit(1)

    fp.parent.mkdir(parents=True, exist_ok=True)
    if not fp.exists():
        fp.write_text(f"# {target.title()}\n\n", encoding="utf-8")

    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or shutil.which("nano") or shutil.which("vi") or "vi"
    print(f"  Opening {fp} with {bold(editor)}...")
    os.execvp(editor, [editor, str(fp)])   # replace process — returns when editor closes


def cmd_inspect(args: list[str]) -> None:
    if not args:
        print(red("Usage: memctl inspect <global|project|conventions|todo>"))
        sys.exit(1)

    target = args[0].lower()
    fp = FILE_ALIASES.get(target)
    if fp is None:
        print(red(f"Unknown target: {target!r}"))
        sys.exit(1)
    if not fp.exists():
        print(yellow(f"File not found: {fp}"))
        return

    content = fp.read_text(encoding="utf-8")
    sections = _parse_sections(content)

    print(bold(f"\n  {fp.name}  —  section analysis\n  {'─' * 54}"))
    if not sections:
        print(yellow("  No ## sections found."))
        return

    for i, s in enumerate(sections, 1):
        badge = _importance_badge(s.importance)
        status = ""
        if s.is_expired:
            status = red(" [EXPIRED]")
        elif s.is_placeholder:
            status = dim(" [template placeholder — not injected]")
        elif s.importance == "temporary" and s.expires:
            status = yellow(f" [expires {s.expires}]")

        body_lines = [l for l in s.content.splitlines() if l.strip() and not l.strip().startswith("<!--")]
        preview = (body_lines[0][:70] + "…") if body_lines and len(body_lines[0]) > 70 else (body_lines[0] if body_lines else dim("(empty)"))

        print(f"\n  {i}. {badge} {bold(s.heading)}{status}")
        print(f"     {dim(preview)}")
        print(f"     {dim(f'{len(s.content)} chars, starts line {s.line_start}')}")

    n_real    = sum(1 for s in sections if not s.is_placeholder)
    n_expired = sum(1 for s in sections if s.is_expired)
    n_perm    = sum(1 for s in sections if s.importance == "permanent" and not s.is_placeholder)
    n_proj    = sum(1 for s in sections if s.importance == "project"   and not s.is_placeholder)
    n_temp    = sum(1 for s in sections if s.importance == "temporary" and not s.is_placeholder)

    print(f"\n  {'─' * 54}")
    print(f"  Sections: {len(sections)} total  •  {n_real} with content  "
          f"({green(f'{n_perm} permanent')}, {cyan(f'{n_proj} project')}, "
          f"{yellow(f'{n_temp} temporary')}{(', ' + red(f'{n_expired} expired')) if n_expired else ''})")
    if n_expired:
        print(f"\n  {yellow('Tip:')} run {bold('memctl clear expired')} to remove expired sections.")
    print()


def cmd_clear(args: list[str]) -> None:
    if not args:
        print(red("Usage: memctl clear <expired|sessions>"))
        sys.exit(1)

    action = args[0].lower()

    if action == "expired":
        _clear_expired()
    elif action == "sessions":
        force = "--yes" in args or "-y" in args
        _clear_sessions(force)
    else:
        print(red(f"Unknown: memctl clear {action}"))
        sys.exit(1)


def _clear_expired() -> None:
    """Remove [TEMPORARY] sections past their expiry date from all memory files."""
    cleared = 0
    for fp in FILE_ALIASES.values():
        if not fp.exists():
            continue
        try:
            original = fp.read_text(encoding="utf-8")
            sections = _parse_sections(original)
            expired = [s for s in sections if s.is_expired]
            if not expired:
                continue

            print(f"  {fp.name}: removing {len(expired)} expired section(s)...")
            # Rebuild file without expired sections
            lines = original.splitlines()
            # Collect line ranges to drop
            drop_ranges: list[tuple[int, int]] = []
            for i, s in enumerate(sections):
                if not s.is_expired:
                    continue
                start = s.line_start - 1   # ## heading line (0-indexed)
                end   = sections[i + 1].line_start - 1 if i + 1 < len(sections) else len(lines)
                drop_ranges.append((start, end))

            # Drop in reverse order to preserve indices
            new_lines = list(lines)
            for start, end in sorted(drop_ranges, reverse=True):
                del new_lines[start:end]

            fp.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
            cleared += len(expired)
            for s in expired:
                print(f"    {red('−')} [{s.importance.upper()}] {s.heading}  {dim(f'(expired {s.expires})')}")
        except Exception as exc:
            print(red(f"  Error processing {fp.name}: {exc}"))

    if cleared == 0:
        print(green("  No expired sections found."))
    else:
        print(green(f"\n  Removed {cleared} expired section(s)."))
        print(f"  Run {bold('memctl rebuild')} to update opencode.json immediately.")


def _clear_sessions(force: bool) -> None:
    if not SESSIONS_DIR.exists():
        print(yellow("  No sessions directory found."))
        return

    files = [f for f in SESSIONS_DIR.glob("*.md") if not f.name.startswith("ARCHIVE_")]
    if not files:
        print(yellow("  No session summaries to clear."))
        return

    if not force:
        ans = input(f"\n  Delete {len(files)} session summary file(s)? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("  Cancelled.")
            return

    for fp in files:
        fp.unlink()
    print(green(f"  Deleted {len(files)} session summary file(s)."))


def cmd_archive() -> None:
    """Force-run session archival now."""
    if not SESSIONS_DIR.exists():
        print(yellow("  No sessions directory found."))
        return

    summaries = [f for f in SESSIONS_DIR.glob("*.md") if not f.name.startswith("ARCHIVE_")]
    n = len(summaries)
    print(f"  {n} session summary file(s) found  (threshold={ARCHIVE_THRESH}, keep={ARCHIVE_KEEP})")

    if n <= ARCHIVE_THRESH:
        print(green(f"  Below threshold — nothing to archive."))
        return

    # Delegate to session_watcher's archive logic by calling it as a module
    try:
        import importlib.util, sys as _sys
        spec = importlib.util.spec_from_file_location("session_watcher", "/session_watcher.py")
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore[attr-defined]
            mod._archive_old_sessions()   # type: ignore[attr-defined]
        else:
            raise ImportError("could not load session_watcher")
    except Exception as exc:
        print(yellow(f"  Could not import session_watcher ({exc}) — running built-in archival..."))
        _builtin_archive(summaries)


def _builtin_archive(summaries: list[Path]) -> None:
    to_archive = sorted(summaries, key=lambda f: f.name)[:-ARCHIVE_KEEP]
    if not to_archive:
        print("  Nothing to archive.")
        return

    by_month: dict[str, list[Path]] = {}
    for fp in to_archive:
        by_month.setdefault(fp.name[:7], []).append(fp)

    for month, fps in sorted(by_month.items()):
        archive_path = SESSIONS_DIR / f"ARCHIVE_{month}.md"
        existing = archive_path.read_text(encoding="utf-8").rstrip() if archive_path.exists() else ""
        entries = []
        for fp in sorted(fps):
            try:
                entries.append(fp.read_text(encoding="utf-8").strip()[:600])
                entries.append("---")
                fp.unlink()
            except Exception:
                continue
        content = (existing + f"\n\n# Archived — {month}\n" + "\n".join(entries)).strip()
        archive_path.write_text(content + "\n", encoding="utf-8")
        print(green(f"  Archived {len(fps)} sessions → {archive_path.name}"))


def cmd_rebuild() -> None:
    """Force immediate re-assembly of memory context into opencode.json."""
    print(f"  Running memory_updater.py once ...")
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("memory_updater", "/memory_updater.py")
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore[attr-defined]
            mod.once()                     # type: ignore[attr-defined]
            if CONFIG_PATH.exists():
                d = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                n = len(d.get("instructions", ""))
                print(green(f"  ✅ opencode.json updated: {n:,} chars in instructions field"))
        else:
            raise ImportError("spec failed")
    except Exception as exc:
        print(yellow(f"  Import failed ({exc}) — falling back to subprocess..."))
        result = subprocess.run(
            [sys.executable, "/memory_updater.py", "once"],
            capture_output=True, text=True
        )
        print(result.stdout or result.stderr or "(no output)")


def cmd_help() -> None:
    print(bold("\n  memctl — OpenCode Memory Management\n"))
    commands = [
        ("status",              "show memory sources, sizes, and section counts"),
        ("show global",         "print GLOBAL.md"),
        ("show project",        "print PROJECT.md"),
        ("show conventions",    "print CONVENTIONS.md"),
        ("show todo",           "print TODO.md"),
        ("show sessions [N]",   "list last N session summaries (default 10)"),
        ("show session <id>",   "print a specific session by ID prefix"),
        ("edit global",         "open GLOBAL.md in $EDITOR"),
        ("edit project",        "open PROJECT.md in $EDITOR"),
        ("edit conventions",    "open CONVENTIONS.md in $EDITOR"),
        ("edit todo",           "open TODO.md in $EDITOR"),
        ("inspect <file>",      "show sections with importance / expiry breakdown"),
        ("clear expired",       "remove [TEMPORARY] sections past their expiry date"),
        ("clear sessions",      "delete all session summaries (prompts for confirm)"),
        ("archive",             "force-archive old sessions now"),
        ("rebuild",             "re-assemble memory → opencode.json immediately"),
    ]
    for cmd, desc in commands:
        print(f"  {bold(f'memctl {cmd}'):<38} {dim(desc)}")
    print()
    print(f"  {dim('Importance tags for memory file sections:')}")
    print(f"  {green('[PERMANENT]')}  coding style, project goals, tech stack")
    print(f"  {cyan('[PROJECT]')}    architecture, APIs, design decisions")
    print(f"  {yellow('[TEMPORARY]')}  active tasks — add <!-- expires: YYYY-MM-DD -->")
    print()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    args = sys.argv[1:]
    if not args:
        cmd_help()
        return

    cmd = args[0].lower()
    rest = args[1:]

    dispatch = {
        "status":   lambda: cmd_status(),
        "show":     lambda: cmd_show(rest),
        "edit":     lambda: cmd_edit(rest),
        "inspect":  lambda: cmd_inspect(rest),
        "clear":    lambda: cmd_clear(rest),
        "archive":  lambda: cmd_archive(),
        "rebuild":  lambda: cmd_rebuild(),
        "help":     lambda: cmd_help(),
        "--help":   lambda: cmd_help(),
        "-h":       lambda: cmd_help(),
    }

    fn = dispatch.get(cmd)
    if fn is None:
        print(red(f"Unknown command: {cmd!r}"))
        cmd_help()
        sys.exit(1)

    fn()


if __name__ == "__main__":
    main()
