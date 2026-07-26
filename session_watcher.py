#!/usr/bin/env python3
"""
OpenCode Session Watcher
========================
Monitors OpenCode's SQLite database for completed conversations and
automatically writes structured summaries to /projects/default/memory/sessions/.

The memory_updater.py watcher picks up new summaries within 15 seconds and
injects them into the system prompt for the next conversation.

This watcher works WITHOUT an LLM — it performs rule-based extraction:
  • User messages (what the user asked/tasked)
  • Files mentioned in the conversation
  • Patterns suggesting architectural or design decisions
  • Tool calls (file edits, commands run)

The AI itself can also write session summaries directly (it is instructed to
do so in the system prompt). This daemon is a complementary automatic fallback
that ensures sessions are captured even when the AI doesn't write one itself.

Usage:
  python3 /session_watcher.py   — run as daemon (blocks forever)

Environment variables:
  SESSION_IDLE_SECS   — minutes of inactivity before session is considered
                        complete (default: 300 = 5 minutes)
  SESSION_MIN_MSGS    — minimum user messages to generate a summary (default: 2)
"""

import json
import os
import re
import sqlite3
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

# ── Configuration ─────────────────────────────────────────────────────────────
DB_PATHS = [
    "/data/share/opencode/opencode.db",
    "/root/.local/share/opencode/opencode.db",
]
SESSIONS_DIR  = Path("/projects/default/memory/sessions")
POLL_SECS     = 30          # how often to poll the DB
IDLE_SECS     = int(os.environ.get("SESSION_IDLE_SECS", "300"))   # 5 min
MIN_USER_MSGS = int(os.environ.get("SESSION_MIN_MSGS", "2"))

# Track which sessions have already been summarised this run
_summarised: set[str] = set()


# ── Logging ───────────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [SESSION] {msg}", flush=True)


# ── DB introspection ──────────────────────────────────────────────────────────

def _find_db() -> str | None:
    for p in DB_PATHS:
        if Path(p).exists():
            return p
    return None


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return [r[1] for r in rows]
    except Exception:
        return []


def _tables(conn: sqlite3.Connection) -> set[str]:
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        return {r[0] for r in rows}
    except Exception:
        return set()


# ── Text extraction ───────────────────────────────────────────────────────────

def _extract_text(raw: str | None) -> str:
    """
    Extract plain text from an OpenCode message content field.
    Content is stored as a JSON array of content-part objects, or plain text.
    """
    if not raw:
        return ""

    # Try JSON array (OpenCode's native format)
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
                        # Include tool names and key inputs for context
                        name = part.get("name", "")
                        inp = part.get("input", {})
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

    # Fallback: treat as plain text
    return str(raw).strip()


# ── File path extraction ──────────────────────────────────────────────────────
# Matches /absolute/paths and relative/paths.ext
_PATH_RE = re.compile(
    r"""(?:^|[\s`"'(])(/(?:[\w.\-]+/)*[\w.\-]+\.\w+)"""
    r"""|(?:^|[\s`"'])([A-Za-z][\w.\-]*/[\w.\-/]+\.\w+)""",
    re.MULTILINE,
)


def _extract_files(text: str) -> list[str]:
    """Extract file paths mentioned in a conversation."""
    found: set[str] = set()
    for m in _PATH_RE.finditer(text):
        p = m.group(1) or m.group(2)
        if p and len(p) < 200:
            found.add(p)
    return sorted(found)


# ── Decision extraction ───────────────────────────────────────────────────────
_DECISION_PATTERNS = re.compile(
    r"(?:we (?:decided|agreed|chose|will use|are going to)|"
    r"the (?:architecture|design|approach|plan|solution) (?:is|will be|uses?)|"
    r"(?:use|using|switched? to|migrated? to|replaced? with)\s+\w+|"
    r"(?:important|key|critical|main):\s|"
    r"going forward|from now on|always |never )",
    re.IGNORECASE,
)


def _extract_decisions(text: str) -> list[str]:
    """Heuristically extract lines that look like architectural decisions."""
    decisions: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if len(line) < 20 or len(line) > 300:
            continue
        if _DECISION_PATTERNS.search(line):
            decisions.append(line)
    return decisions[:8]  # cap at 8 per session


# ── Summary writer ────────────────────────────────────────────────────────────

def _write_summary(session_id: str, title: str | None, created_ms: int,
                   user_messages: list[str], assistant_messages: list[str],
                   workspace: str | None) -> bool:
    """
    Generate and write a session summary markdown file.
    Returns True on success.
    """
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    # Derive timestamp from session creation time (milliseconds or seconds)
    try:
        ts_secs = created_ms / 1000 if created_ms > 9_999_999_999 else created_ms
        dt = datetime.fromtimestamp(ts_secs, tz=timezone.utc)
    except Exception:
        dt = datetime.now(tz=timezone.utc)

    date_str = dt.strftime("%Y-%m-%d")
    time_str = dt.strftime("%H-%M")
    short_id = session_id[:8] if len(session_id) >= 8 else session_id

    filename = f"{date_str}_{time_str}_{short_id}.md"
    out_path  = SESSIONS_DIR / filename

    # Skip if a file for this session already exists
    if out_path.exists():
        return False

    # ── Build summary ────────────────────────────────────────────────────────
    all_user_text = "\n".join(user_messages)
    all_text      = "\n".join(user_messages + assistant_messages)

    # Topics: first line of each user message (question or task)
    topics: list[str] = []
    for msg in user_messages[:8]:
        first_line = msg.split("\n")[0].strip()
        if first_line and len(first_line) > 5:
            topics.append(first_line[:200])

    # Files mentioned
    files = _extract_files(all_text)[:15]

    # Decisions
    decisions = _extract_decisions(all_text)

    # Build the markdown
    lines: list[str] = [
        f"# Session {dt.strftime('%Y-%m-%d %H:%M')} UTC",
        f"**Session ID:** `{short_id}`",
    ]
    if title:
        lines.append(f"**Title:** {title}")
    if workspace:
        lines.append(f"**Workspace:** `{workspace}`")
    lines.append(f"**Messages:** {len(user_messages)} user, {len(assistant_messages)} assistant")
    lines.append("")

    if topics:
        lines.append("## Topics / Tasks")
        for t in topics:
            lines.append(f"- {t}")
        lines.append("")

    if decisions:
        lines.append("## Key Decisions & Observations")
        for d in decisions:
            lines.append(f"- {d}")
        lines.append("")

    if files:
        lines.append("## Files Referenced")
        for f in files:
            lines.append(f"- `{f}`")
        lines.append("")

    # Compact excerpt of the conversation flow (first 2 + last 2 user msgs)
    if len(user_messages) > 4:
        excerpt_msgs = user_messages[:2] + ["..."] + user_messages[-2:]
    else:
        excerpt_msgs = user_messages

    lines.append("## Conversation Flow")
    for i, msg in enumerate(excerpt_msgs):
        if msg == "...":
            lines.append("- *(earlier messages omitted)*")
        else:
            preview = msg[:300].replace("\n", " ").strip()
            lines.append(f"- **User:** {preview}")
    lines.append("")
    lines.append("---")
    lines.append("*Auto-generated by session_watcher. The AI may also have written its own summary above.*")

    content = "\n".join(lines)

    try:
        out_path.write_text(content, encoding="utf-8")
        _log(f"✅ Summary written: memory/sessions/{filename}")
        return True
    except Exception as exc:
        _log(f"Could not write summary {filename}: {exc}")
        return False


# ── Session processing ────────────────────────────────────────────────────────

def _process_db(db_path: str) -> None:
    """
    Scan the DB for sessions that:
      1. Have not been summarised yet
      2. Have been idle for at least IDLE_SECS
      3. Have at least MIN_USER_MSGS user messages

    For each qualifying session, generate and write a summary.
    """
    try:
        conn = sqlite3.connect(db_path, timeout=5, check_same_thread=False)
        conn.row_factory = sqlite3.Row
    except Exception as exc:
        _log(f"Cannot open DB {db_path}: {exc}")
        return

    try:
        available_tables = _tables(conn)

        if "session" not in available_tables:
            conn.close()
            return

        session_cols = _table_columns(conn, "session")

        # Detect timestamp column name
        ts_col = None
        for candidate in ("updated_at", "created_at", "last_active", "modified_at"):
            if candidate in session_cols:
                ts_col = candidate
                break

        created_col = "created_at" if "created_at" in session_cols else ts_col
        title_col   = "title" if "title" in session_cols else None
        dir_col     = "directory" if "directory" in session_cols else None

        if not ts_col:
            conn.close()
            return

        # Find message table
        msg_table = None
        for candidate in ("message", "messages", "chat_message"):
            if candidate in available_tables:
                msg_table = candidate
                break

        now_ms = time.time() * 1000  # use ms; DB may store ms or s
        cutoff = now_ms - (IDLE_SECS * 1000)

        # Fetch all sessions that haven't been updated recently
        sessions = conn.execute(
            f"SELECT * FROM session WHERE {ts_col} < ? ORDER BY {ts_col} DESC LIMIT 100",
            (cutoff,),
        ).fetchall()

        # Also check if the DB stores timestamps in seconds (not ms)
        if not sessions:
            cutoff_s = time.time() - IDLE_SECS
            sessions = conn.execute(
                f"SELECT * FROM session WHERE {ts_col} < ? ORDER BY {ts_col} DESC LIMIT 100",
                (cutoff_s,),
            ).fetchall()

        for session in sessions:
            sid = session["id"]
            if sid in _summarised:
                continue

            # Check if AI already wrote a summary for this session
            short_id = sid[:8] if len(sid) >= 8 else sid
            existing = list(SESSIONS_DIR.glob(f"*_{short_id}.md")) if SESSIONS_DIR.exists() else []
            if existing:
                _summarised.add(sid)
                continue

            # Extract messages if the table exists
            user_msgs: list[str]       = []
            assistant_msgs: list[str]  = []

            if msg_table:
                msg_cols = _table_columns(conn, msg_table)
                session_fk = None
                for candidate in ("session_id", "sessionId", "conversation_id"):
                    if candidate in msg_cols:
                        session_fk = candidate
                        break

                role_col    = "role"    if "role"    in msg_cols else None
                content_col = "content" if "content" in msg_cols else None
                parts_col   = "parts"   if "parts"   in msg_cols else content_col

                if session_fk and content_col:
                    messages = conn.execute(
                        f"SELECT * FROM {msg_table} WHERE {session_fk} = ? ORDER BY rowid",
                        (sid,),
                    ).fetchall()

                    for msg in messages:
                        raw = msg[content_col] if content_col in msg.keys() else ""
                        text = _extract_text(raw)
                        if not text:
                            continue
                        role = msg[role_col].lower() if role_col and msg[role_col] else "unknown"
                        if role in ("user", "human"):
                            user_msgs.append(text)
                        elif role in ("assistant", "ai", "model"):
                            assistant_msgs.append(text)

            # Skip short sessions
            if len(user_msgs) < MIN_USER_MSGS:
                _summarised.add(sid)
                continue

            title     = session[title_col]  if title_col  else None
            workspace = session[dir_col]    if dir_col    else None
            created   = session[created_col] if created_col else 0

            try:
                created = int(created) if created else 0
            except (ValueError, TypeError):
                created = 0

            _write_summary(
                session_id=sid,
                title=title,
                created_ms=created,
                user_messages=user_msgs,
                assistant_messages=assistant_msgs,
                workspace=workspace,
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
    _log(f"Session watcher started (idle_secs={IDLE_SECS}, min_msgs={MIN_USER_MSGS})")
    _log(f"Writing summaries to: {SESSIONS_DIR}")
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    while True:
        try:
            db = _find_db()
            if db:
                _process_db(db)
        except Exception as exc:
            _log(f"Main loop error: {exc}")
        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
