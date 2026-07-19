"""Manages per-workspace opencode serve processes.

Each workspace gets its own opencode serve on a dynamic port.
Processes are started on demand and shut down after idle timeout.
"""
import asyncio
import logging
import os
import signal
import time
from typing import Optional

from services.process_registry import get_registry

logger = logging.getLogger(__name__)

IDLE_TIMEOUT_SECONDS = 30 * 60  # 30 minutes
PORT_RANGE_MIN = 4100
PORT_RANGE_MAX = 4999
OPENCODE_USERNAME = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
OPENCODE_PASSWORD = os.environ.get("OPENCODE_SERVER_PASSWORD", "password")


class WorkspaceProcessManager:
    def __init__(self):
        self.registry = get_registry()
        self._check_task: Optional[asyncio.Task] = None

    def allocate_port(self) -> int:
        used = {ws["port"] for ws in self.registry.list_all() if ws.get("port")}
        for port in range(PORT_RANGE_MIN, PORT_RANGE_MAX + 1):
            if port not in used:
                return port
        raise RuntimeError("No free ports available")

    def get_or_create_workspace(self, user_id: str, name: str, path: str) -> dict:
        ws_id = f"user_{user_id}_{name}"
        existing = self.registry.get(ws_id)
        if existing:
            return existing
        return self.registry.create(ws_id, name, path, user_id=user_id)

    async def start(self, workspace_id: str) -> dict:
        ws = self.registry.get(workspace_id)
        if not ws:
            raise ValueError(f"Workspace {workspace_id} not found")

        # Check if already running
        if ws.get("status") == "running" and ws.get("pid"):
            try:
                os.kill(ws["pid"], 0)
                self.registry.update(workspace_id, lastActive=int(time.time()))
                return {"port": ws["port"], "pid": ws["pid"], "alreadyRunning": True}
            except OSError:
                pass  # Process dead, restart

        port = self.allocate_port()
        workspace_path = ws["path"]

        os.makedirs(workspace_path, exist_ok=True)

        proc = await asyncio.create_subprocess_exec(
            "opencode", "serve", "--port", str(port), "--hostname", "127.0.0.1",
            cwd=workspace_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        self.registry.update(
            workspace_id,
            port=port,
            pid=proc.pid,
            status="running",
            lastActive=int(time.time()),
        )

        logger.info(f"Started opencode serve for {workspace_id} on port {port} (PID: {proc.pid})")

        # Wait for the process to bind to the port
        import urllib.request
        for i in range(30):
            await asyncio.sleep(1)
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{port}/global/health")
                resp = urllib.request.urlopen(req, timeout=2)
                if resp.status == 200:
                    logger.info(f"opencode serve ready on port {port} (attempt {i+1})")
                    break
            except Exception:
                if i == 29:
                    logger.warning(f"opencode serve not ready after 30s on port {port}")
                continue

        return {"port": port, "pid": proc.pid}

    async def stop(self, workspace_id: str):
        ws = self.registry.get(workspace_id)
        if not ws:
            return

        pid = ws.get("pid")
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
            # Force kill after 5s if still alive
            await asyncio.sleep(5)
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

        self.registry.update(workspace_id, status="stopped", pid=None, port=None)
        logger.info(f"Stopped opencode serve for {workspace_id}")

    async def ensure_running(self, workspace_id: str) -> dict:
        ws = self.registry.get(workspace_id)
        if not ws:
            raise ValueError(f"Workspace {workspace_id} not found")

        if ws.get("status") == "running" and ws.get("pid"):
            try:
                os.kill(ws["pid"], 0)
                self.registry.update(workspace_id, lastActive=int(time.time()))
                return {"port": ws["port"], "pid": ws["pid"]}
            except OSError:
                pass

        return await self.start(workspace_id)

    async def shutdown_idle(self):
        now = int(time.time())
        for ws in self.registry.list_all():
            if ws.get("status") == "running" and ws.get("lastActive"):
                if now - ws["lastActive"] > IDLE_TIMEOUT_SECONDS:
                    logger.info(f"Shutting down idle workspace: {ws['name']} ({ws['id']})")
                    await self.stop(ws["id"])

    async def shutdown_all(self):
        for ws in self.registry.list_all():
            if ws.get("status") == "running":
                await self.stop(ws["id"])

    def start_idle_checker(self):
        if self._check_task is None or self._check_task.done():
            self._check_task = asyncio.create_task(self._idle_check_loop())

    async def _idle_check_loop(self):
        while True:
            await asyncio.sleep(60)
            await self.shutdown_idle()

    def get_auth_header(self) -> str:
        import base64
        cred = f"{OPENCODE_USERNAME}:{OPENCODE_PASSWORD}"
        return "Basic " + base64.b64encode(cred.encode()).decode()


# Singleton
_manager: Optional[WorkspaceProcessManager] = None


def get_workspace_manager() -> WorkspaceProcessManager:
    global _manager
    if _manager is None:
        _manager = WorkspaceProcessManager()
    return _manager
