#!/usr/bin/env python3
"""
OpenCode Session Watcher  (v2)
================================
Two responsibilities:
  1. Summarise completed conversations from OpenCode's SQLite DB (fallback —
     the AI is the primary summariser via its system-prompt instructions).
  2. Archive old session summaries before memory/sessions/ grows unboundedly.

DB polling is kept as a robust fallback.  If OpenCode changes its storage
mechanism in a future release, the archival and AI-driven paths still work.

Usage:
  python3 /session_watcher.py   — run as daemon (blocks forever)

Environment:
  SESSION_IDLE_SECS   — inactivity before session is "complete" (default 300)
  SESSION_MIN_MSGS    — minimum user messages to summarise (default 2)
  SESSION_ARCHIVE_N   — archive when sessions/ has more than N summaries (default 25)
  SESSION_KEEP_N      — keep this many recent summaries unarchived (default 10)
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Configuration ─────────────────────────────────────────────────────────────
DB_PATHS = [
    "/data/share/opencode/opencode.db",
    "/root/.local/share/opencode/opencode.db",
]
SESSIONS_DIR    = Path("/projects/default/memory/sessions")
POLL_SECS       = 30
IDLE_SECS       = int(os.environ.get("SESSION_IDLE_SECS",    "300"))
MIN_USER_MSGS   = int(os.environ.get("SESSION_MIN_MSGS",     "2"))
ARCHIVE_THRESH  = int(os.environ.get("SESSION_ARCHIVE_N",    "25"))
ARCHIVE_KEEP    = int(os.environ.get("SESSION_KEEP_N",       "10"))

# Sessions already handled this daemon run
_summarised: set[str] = set()

# Maintenance cadence: run archival every N poll cycles (~10 min at 30s poll)
_ARCHIVE_EVERY_N_CYCLES = 20
_cycle = 0


# ── Logging ───────────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [SESSION] {msg}", flush=True)


# ── DB helpers ────────────────────────────────────────────────────────────────

def _find_db() -> Optional[str]:
    for p in DB_PATHS:
        if Path(p).exists():
            return p
    return None


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    except Exception:
        return []


def _tables(conn: sqlite3.Connection) -> set[str]:
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    except Exception:
        return set()


# ── Text extraction ───────────────────────────────────────────────────────────

def _extract_text(raw: Optional[str]) -> str:
    """Extract plain text from an OpenCode message content field (JSON or plain)."""
    if not raw:
        return ""
    try:
        parts = json.loads(raw)
        if isinstance(parts, list):
            texts: list[str] = []
            for part in parts:
                if isinstance(part, dict):
                    t = part.get("type", "")
                    if t == "text":
                        texts.append(part.get("text", ""))
                    elif t == "tool_use":
                        name = part.get("name", "")
                        inp  = part.get("input", {})
                        if isinstance(inp, dict):
                            if "command" in inp:
                                texts.append(f"[ran: {inp['command']}]")
                            elif "path" in inp:
                                texts.append(f"[file: {inp['path']}]")
                            elif name:
                                texts.append(f"[tool: {name}]")
                elif isinstance(part, str):
                    texts.append(part)
            return " ".join(t.strip() for t in texts if t.strip())
        elif isinstance(parts, str):
            return parts
    except (json.JSONDecodeError, TypeError):
        pass
    return str(raw).strip()


_PATH_RE = re.compile(
    r"(?:^|[\s`\"'(])(/(?:[\w.\-]+/)*[\w.\-]+\.\w+)"
    r"|(?:^|[\s`\"'])([A-Za-z][\w.\-]*/[\w.\-/]+\.\w+)",
    re.MULTILINE,
)

_DECISION_RE = re.compile(
    r"(?:we (?:decided|agreed|chose|will use|are going to)|"
    r"the (?:architecture|design|approach|plan|solution) (?:is|will be|uses?)|"
    r"(?:use|using|switched? to|migrated? to|replaced? with)\s+\w+|"
    r"(?:important|key|critical|main):\s|"
    r"going forward|from now on|always |never )",
    re.IGNORECASE,
)


def _extract_files(text: str) -> list[str]:
    found: set[str] = set()
    for m in _PATH_RE.finditer(text):
        p = m.group(1) or m.group(2)
        if p and len(p) < 200:
            found.add(p)
    return sorted(found)


def _extract_decisions(text: str) -> list[str]:
    decisions: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if 20 <= len(line) <= 300 and _DECISION_RE.search(line):
            decisions.append(line)
    return decisions[:8]


# ── Summary writer ────────────────────────────────────────────────────────────

def _write_summary(
    session_id: str,
    title: Optional[str],
    created_ts: int,
    user_messages: list[str],
    assistant_messages: list[str],
    workspace: Optional[str],
) -> bool:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    try:
        ts_secs = created_ts / 1000 if created_ts > 9_999_999_999 else created_ts
        dt = datetime.fromtimestamp(ts_secs, tz=timezone.utc)
    except Exception:
        dt = datetime.now(tz=timezone.utc)

    short_id = session_id[:8]
    filename = f"{dt.strftime('%Y-%m-%d_%H-%M')}_{short_id}.md"
    out_path = SESSIONS_DIR / filename

    if out_path.exists():
        return False

    all_text = "\n".join(user_messages + assistant_messages)

    topics: list[str] = []
    for msg in user_messages[:8]:
        first = msg.split("\n")[0].strip()
        if len(first) > 5:
            topics.append(first[:200])

    files     = _extract_files(all_text)[:15]
    decisions = _extract_decisions(all_text)

    lines: list[str] = [
        f"# Session {dt.strftime('%Y-%m-%d %H:%M')} UTC",
        f"**ID:** `{short_id}`",
    ]
    if title:
        lines.append(f"**Title:** {title}")
    if workspace:
        lines.append(f"**Workspace:** `{workspace}`")
    lines.append(f"**Turns:** {len(user_messages)} user / {len(assistant_messages)} assistant")
    lines.append("")

    if topics:
        lines += ["## Topics / Tasks", *[f"- {t}" for t in topics], ""]
    if decisions:
        lines += ["## Key Decisions", *[f"- {d}" for d in decisions], ""]
    if files:
        lines += ["## Files Referenced", *[f"- `{f}`" for f in files], ""]

    # Compact conversation flow
    sample = (user_messages[:2] + (["*(earlier messages omitted)*"] if len(user_messages) > 4 else []) + user_messages[-2:]) if len(user_messages) > 4 else user_messages
    lines.append("## Conversation Flow")
    for msg in sample:
        if msg.startswith("*"):
            lines.append(f"- {msg}")
        else:
            lines.append(f"- **User:** {msg[:300].replace(chr(10), ' ')}")
    lines += ["", "---",
              "*Auto-generated by session_watcher (DB fallback). "
              "The AI may also have written its own summary above.*"]

    try:
        out_path.write_text("\n".join(lines), encoding="utf-8")
        _log(f"✅ Summary: memory/sessions/{filename}")
        return True
    except Exception as exc:
        _log(f"Could not write {filename}: {exc}")
        return False


# ── Session archival ──────────────────────────────────────────────────────────

def _extract_archive_snippet(content: str, stem: str) -> str:
    """
    Condense one session summary to a compact archive entry.
    Keeps the title line + Topics/Tasks + Decisions sections (≤20 lines).
    """
    lines = content.splitlines()
    keep: list[str] = []
    in_section = False
    capture_sections = {"## Topics / Tasks", "## Key Decisions", "## Decisions Made", "## Accomplished"}

    for line in lines:
        if line.startswith("# "):          # session title
            keep.append(line)
        elif line in capture_sections:
            in_section = True
            keep.append(line)
        elif line.startswith("## ") and in_section:
            in_section = False             # stop at next section
        elif in_section and line.startswith("- "):
            keep.append(line)

    if len(keep) > 20:
        keep = keep[:20]
        keep.append("  *(truncated)*")

    return "\n".join(keep) if keep else f"*(session {stem})*"


def _archive_old_sessions() -> None:
    """
    When memory/sessions/ has more than ARCHIVE_THRESH summary files,
    merge the oldest batch into monthly ARCHIVE_YYYY-MM.md files and
    delete the originals.  Archive files are excluded from injection
    (they don't match the "newest N" selection in memory_updater.py).
    """
    if not SESSIONS_DIR.exists():
        return

    all_files = sorted(SESSIONS_DIR.glob("*.md"), key=lambda f: f.name)
    # Separate real summaries from existing archives
    summaries = [f for f in all_files if not f.name.startswith("ARCHIVE_")]

    if len(summaries) <= ARCHIVE_THRESH:
        return

    # Archive everything older than the most recent ARCHIVE_KEEP files
    to_archive = summaries[:-ARCHIVE_KEEP] if len(summaries) > ARCHIVE_KEEP else []
    if not to_archive:
        return

    _log(f"Archiving {len(to_archive)} old session summaries (total={len(summaries)}, "
         f"threshold={ARCHIVE_THRESH}, keep={ARCHIVE_KEEP})...")

    # Group by year-month (filename prefix YYYY-MM)
    by_month: dict[str, list[Path]] = {}
    for fp in to_archive:
        month = fp.name[:7]   # "YYYY-MM"
        by_month.setdefault(month, []).append(fp)

    archived = 0
    for month, fps in sorted(by_month.items()):
        archive_path = SESSIONS_DIR / f"ARCHIVE_{month}.md"

        # Load existing archive content
        existing = ""
        if archive_path.exists():
            try:
                existing = archive_path.read_text(encoding="utf-8").rstrip()
            except Exception:
                pass

        new_entries: list[str] = []
        for fp in sorted(fps, key=lambda f: f.name):
            try:
                content = fp.read_text(encoding="utf-8").strip()
                snippet = _extract_archive_snippet(content, fp.stem)
                new_entries.append(snippet)
                new_entries.append("---")
            except Exception:
                continue

        if not new_entries:
            continue

        header = f"\n\n# Archived Sessions — {month}\n"
        combined = (existing + header + "\n".join(new_entries)).strip()

        try:
            archive_path.write_text(combined + "\n", encoding="utf-8")
            for fp in fps:
                fp.unlink()
                archived += 1
            _log(f"  → ARCHIVE_{month}.md  ({len(fps)} sessions merged)")
        except Exception as exc:
            _log(f"  Archive write failed for {month}: {exc}")

    _log(f"Archival complete: {archived} files merged into monthly archives")


# ── DB processing ─────────────────────────────────────────────────────────────

def _process_db(db_path: str) -> None:
    """Scan DB for completed sessions and write summaries for qualifying ones."""
    try:
        conn = sqlite3.connect(db_path, timeout=5, check_same_thread=False)
        conn.row_factory = sqlite3.Row
    except Exception as exc:
        _log(f"Cannot open DB {db_path}: {exc}")
        return

    try:
        available = _tables(conn)
        if "session" not in available:
            return

        session_cols = _table_columns(conn, "session")

        # Detect timestamp, title, directory columns
        ts_col      = next((c for c in ("updated_at", "created_at", "last_active") if c in session_cols), None)
        created_col = "created_at" if "created_at" in session_cols else ts_col
        title_col   = "title"     if "title"     in session_cols else None
        dir_col     = "directory" if "directory" in session_cols else None

        if not ts_col:
            return

        # Find message table
        msg_table = next((t for t in ("message", "messages", "chat_message") if t in available), None)

        # Try both ms and s timestamps
        now_ms = time.time() * 1000
        cutoff_ms = now_ms - (IDLE_SECS * 1000)
        cutoff_s  = time.time() - IDLE_SECS

        sessions = conn.execute(
            f"SELECT * FROM session WHERE {ts_col} < ? ORDER BY {ts_col} DESC LIMIT 200",
            (cutoff_ms,),
        ).fetchall()
        if not sessions:
            sessions = conn.execute(
                f"SELECT * FROM session WHERE {ts_col} < ? ORDER BY {ts_col} DESC LIMIT 200",
                (cutoff_s,),
            ).fetchall()

        for session in sessions:
            sid = session["id"]
            if sid in _summarised:
                continue

            short_id = sid[:8]
            # Skip if AI already wrote a summary for this session
            if list(SESSIONS_DIR.glob(f"*_{short_id}.md")):
                _summarised.add(sid)
                continue

            user_msgs: list[str]      = []
            assistant_msgs: list[str] = []

            if msg_table:
                msg_cols   = _table_columns(conn, msg_table)
                session_fk = next((c for c in ("session_id", "sessionId", "conversation_id") if c in msg_cols), None)
                role_col   = "role"    if "role"    in msg_cols else None
                content_col= "content" if "content" in msg_cols else None

                if session_fk and content_col:
                    for msg in conn.execute(
                        f"SELECT * FROM {msg_table} WHERE {session_fk} = ? ORDER BY rowid",
                        (sid,),
                    ).fetchall():
                        text = _extract_text(msg[content_col])
                        if not text:
                            continue
                        role = (msg[role_col] or "").lower() if role_col else ""
                        if role in ("user", "human"):
                            user_msgs.append(text)
                        elif role in ("assistant", "ai", "model"):
                            assistant_msgs.append(text)

            if len(user_msgs) < MIN_USER_MSGS:
                _summarised.add(sid)
                continue

            created = 0
            if created_col:
                try:
                    created = int(session[created_col] or 0)
                except (ValueError, TypeError):
                    pass

            _write_summary(
                session_id=sid,
                title=session[title_col] if title_col else None,
                created_ts=created,
                user_messages=user_msgs,
                assistant_messages=assistant_msgs,
                workspace=session[dir_col] if dir_col else None,
            )
            _summarised.add(sid)

    except Exception as exc:
        _log(f"DB processing error: {exc}")
        _log(traceback.format_exc())
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    global _cycle
    _log(f"Session watcher started  "
         f"(idle={IDLE_SECS}s, min_msgs={MIN_USER_MSGS}, "
         f"archive_at={ARCHIVE_THRESH}, keep={ARCHIVE_KEEP})")
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    # Run archival once on startup to catch any pre-existing overflow
    _archive_old_sessions()

    while True:
        try:
            _cycle += 1

            db = _find_db()
            if db:
                _process_db(db)

            # Periodic maintenance
            if _cycle % _ARCHIVE_EVERY_N_CYCLES == 0:
                _archive_old_sessions()

        except Exception as exc:
            _log(f"Main loop error: {exc}")
        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
