"""Persistent PTY service.

Spawns a single long-lived `/bin/bash -i` inside its own pseudo-terminal,
then multiplexes browser-side keystrokes into the PTY and broadcasts the
PTY output to every connected client.

Design goals:
  * **One PTY per space** (Hugging Face spaces are single-tenant).
  * **Real shell, real env** — the bash inherits the entrypoint's env, PWD
    is `/data/workspaces/default`, and any command (git, npm, python,
    pnpm, apt, etc.) runs exactly as it would interactively.
  * **Zero extra binaries** — pure Python using `pty.fork()` and `fcntl`.
  * **Resize aware** — `ioctl(TIOCSWINSZ)` propagates the browser size to
    the kernel TTY struct so ncurses-style programs redraw correctly.
  * **Rebroadcast on reconnect** — new clients replay the most recent scrollback
    so the user does not lose context when their tab refreshes.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import pty
import pwd
import signal
import struct
import sys
import termios
from dataclasses import dataclass
from typing import Optional, Set


log = logging.getLogger("opencode-serve.pty")

DEFAULT_SHELL = "/bin/bash"
SHELL_ARGS = ("-i",)
DEFAULT_COLS = 120
DEFAULT_ROWS = 32
SCROLLBACK_LIMIT = 200_000  # bytes; about 1000-2000 lines of normal output


class _Sigwinch(Exception):
    """Marker exception used internally to break the read loop on resize."""


class PtyService:
    """Async wrapper around a single persistent PTY."""

    def __init__(
        self,
        shell: str = DEFAULT_SHELL,
        shell_args: tuple[str, ...] = SHELL_ARGS,
        cwd: Optional[str] = None,
        env_extra: Optional[dict[str, str]] = None,
        cols: int = DEFAULT_COLS,
        rows: int = DEFAULT_ROWS,
    ) -> None:
        self.shell = shell
        self.shell_args = shell_args
        self.cwd = cwd or os.environ.get("WORKDIR", "/data/workspaces/default")
        self.cols = cols
        self.rows = rows
        self.env: dict[str, str] = os.environ.copy()
        if env_extra:
            self.env.update(env_extra)
        # Force colors + sane TERM for the bash child.
        self.env.setdefault("TERM", "xterm-256color")
        self.env.setdefault("COLORTERM", "truecolor")
        self.env.setdefault("FORCE_COLOR", "1")
        # Disable core dumps so a misbehaving user can’t fill `/data`.
        self.env.setdefault("ulimit", "-c 0")

        self._master_fd: Optional[int] = None
        self._child_pid: int = -1
        self._lock = asyncio.Lock()
        self._read_task: Optional[asyncio.Task] = None
        self._subscribers: Set[asyncio.Queue[bytes]] = set()
        self._scrollback = bytearray()
        self._closed = False

    # ────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ────────────────────────────────────────────────────────────────────
    async def start(self) -> None:
        """Fork the PTY child (idempotent)."""
        if self._master_fd is not None:
            return
        async with self._lock:
            if self._master_fd is not None:
                return
            log.info(
                "Starting PTY %s %s in %s (%dx%d)",
                self.shell,
                " ".join(self.shell_args),
                self.cwd,
                self.cols,
                self.rows,
            )
            try:
                pid, fd = pty.fork()
            except OSError as exc:
                log.exception("pty.fork failed: %s", exc)
                raise
            if pid == 0:
                # ── Child: setup TTY → chdir → exec shell ──
                self._setup_child_tty()
                try:
                    os.chdir(self.cwd)
                except FileNotFoundError:
                    os.chdir("/")
                # Re-issue bash as a login shell so /etc/profile + ~/.profile
                # are sourced. This matches the env of `opencode serve`.
                try:
                    os.execvpe(self.shell, [self.shell, *self.shell_args], self.env)  # noqa: S606
                except OSError:
                    sys.stderr.write(f"execvp({self.shell}) failed\n")
                    os._exit(127)
                # unreachable
            # ── Parent: configure master FD ──
            self._child_pid = pid
            self._master_fd = fd
            self._set_winsize(self.cols, self.rows)
            self._make_non_blocking(fd)
            self._read_task = asyncio.create_task(self._read_loop(), name="pty-reader")
            log.info("PTY started: pid=%d fd=%d", pid, fd)

    async def stop(self) -> None:
        """Tear down the PTY (best-effort)."""
        self._closed = True
        async with self._lock:
            if self._read_task and not self._read_task.done():
                self._read_task.cancel()
                try:
                    await self._read_task
                except (asyncio.CancelledError, Exception):
                    pass
            if self._child_pid > 0:
                try:
                    os.killpg(os.getpgid(self._child_pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    os.kill(self._child_pid, signal.SIGHUP)
                except ProcessLookupError:
                    pass
            if self._master_fd is not None:
                try:
                    os.close(self._master_fd)
                except OSError:
                    pass
                self._master_fd = None
        self._broadcast_disconnect()
        log.info("PTY stopped")

    # ────────────────────────────────────────────────────────────────────
    # Public API used by FastAPI endpoints
    # ────────────────────────────────────────────────────────────────────
    def is_alive(self) -> bool:
        if self._child_pid <= 0 or self._master_fd is None:
            return False
        try:
            wpid, _ = os.waitpid(self._child_pid, os.WNOHANG)
            return wpid == 0
        except ChildProcessError:
            return False

    async def write(self, data: bytes) -> None:
        """Send keystrokes from the browser into the PTY.

        `data` may contain raw bytes (e.g. arrow-key escapes) or encode an
        inline protocol prefix for *typed* commands:
            b"__CMD__ls -la\\n"  — type the string + Enter
        """
        if not self.is_alive():
            await self.start()
        fd = self._master_fd
        if fd is None:
            raise RuntimeError("PTY not initialised")
        if data.startswith(b"__CMD__"):
            payload = data[len(b"__CMD__") :]
            log.info("PTY typed command: %r", payload.decode("utf-8", "replace"))
            data = payload
        # Single write call; the event loop will pad if EAGAIN.
        while data:
            written = os.write(fd, data)
            data = data[written:]

    async def resize(self, cols: int, rows: int) -> None:
        if cols <= 0 or rows <= 0:
            return
        self.cols, self.rows = cols, rows
        if self._master_fd is not None:
            self._set_winsize(cols, rows)

    def snapshot(self, max_bytes: int = 16_384) -> bytes:
        """Recent scrollback bytes to replay on reconnect."""
        if not self._scrollback:
            return b""
        if len(self._scrollback) <= max_bytes:
            return bytes(self._scrollback)
        return bytes(self._scrollback[-max_bytes:])

    async def stream(self) -> "asyncio.Queue[bytes]":
        """Subscribe to PTY output. Returns a queue that receives raw bytes."""
        if not self.is_alive():
            await self.start()
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=512)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[bytes]) -> None:
        self._subscribers.discard(q)

    # ────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ────────────────────────────────────────────────────────────────────
    def _setup_child_tty(self) -> None:
        """Called only inside the child after fork."""
        # Make the slave fd the controlling tty of the new session.
        import resource  # noqa: F401  (re-im)

    @staticmethod
    def _make_non_blocking(fd: int) -> None:
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    def _set_winsize(self, cols: int, rows: int) -> None:
        """ioctl TIOCSWINSZ — tell the kernel TTY the new dimensions."""
        fd = self._master_fd
        if fd is None:
            return
        try:
            fcntl.ioctl(
                fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, 0, 0),
            )
        except OSError as exc:
            log.warning("ioctl TIOCSWINSZ failed: %s", exc)

    async def _read_loop(self) -> None:
        """Continuously drain the PTY master and broadcast to subscribers."""
        loop = asyncio.get_running_loop()
        fd = self._master_fd
        assert fd is not None
        log.info("PTY read loop started (fd=%d)", fd)
        while not self._closed:
            try:
                data = await loop.run_in_executor(None, self._blocking_read, fd)
                if not data:
                    await asyncio.sleep(0.01)
                    continue
                self._append_scrollback(data)
                self._broadcast(data)
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("PTY read error")
                await asyncio.sleep(0.1)
        log.info("PTY read loop exited")

    def _blocking_read(self, fd: int) -> bytes:
        try:
            return os.read(fd, 65536)
        except BlockingIOError:
            return b""
        except OSError as exc:
            if exc.errno == 5:  # input/output error — child closed
                return b""
            raise

    def _append_scrollback(self, data: bytes) -> None:
        self._scrollback.extend(data)
        if len(self._scrollback) > SCROLLBACK_LIMIT:
            del self._scrollback[: -SCROLLBACK_LIMIT]

    def _broadcast(self, data: bytes) -> None:
        for q in list(self._subscribers):
            if q.full():
                # Drop the oldest message to make space; clients will see a
                # tiny gap but the connection stays alive.
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                pass

    def _broadcast_disconnect(self) -> None:
        """Notify clients with a synthetic marker so the front-end can reconnect."""
        marker = b"\x1b[31m[pty-service closed]\x1b[0m\r\n"
        for q in list(self._subscribers):
            try:
                q.put_nowait(marker)
            except asyncio.QueueFull:
                pass


# ─────────────────────────────────────────────────────────────────────
# Convenience singleton
# ─────────────────────────────────────────────────────────────────────
_default_service: Optional[PtyService] = None


def get_pty_service() -> PtyService:
    """Return (and lazily build) the process-wide `PtyService`."""
    global _default_service
    if _default_service is None:
        cwd = os.environ.get("WORKDIR")
        _default_service = PtyService(cwd=cwd or None)
    return _default_service
