"""JSON-backed registry for tracking workspace processes.

Each workspace entry stores:
  id, name, path, port, pid, status, lastActive, createdAt
"""
import json
import os
import time
from typing import Optional


class ProcessRegistry:
    def __init__(self, file_path: str = "/data/workspaces.json"):
        self.file_path = file_path
        self.workspaces: dict = {}
        self._load()

    def _load(self):
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r") as f:
                    self.workspaces = json.load(f)
            except (json.JSONDecodeError, IOError):
                self.workspaces = {}
        else:
            self.workspaces = {}

    def save(self):
        os.makedirs(os.path.dirname(self.file_path) or ".", exist_ok=True)
        with open(self.file_path, "w") as f:
            json.dump(self.workspaces, f, indent=2)

    def list_all(self) -> list[dict]:
        return list(self.workspaces.values())

    def get(self, workspace_id: str) -> Optional[dict]:
        return self.workspaces.get(workspace_id)

    def get_by_user(self, user_id: str) -> list[dict]:
        prefix = f"user_{user_id}_"
        return [
            ws for ws in self.workspaces.values()
            if ws.get("id", "").startswith(prefix) or ws.get("user_id") == user_id
        ]

    def create(self, workspace_id: str, name: str, path: str,
               user_id: str = None, source: str = None, source_type: str = "local") -> dict:
        self.workspaces[workspace_id] = {
            "id": workspace_id,
            "name": name,
            "path": path,
            "user_id": user_id,
            "source": source,
            "sourceType": source_type,
            "port": None,
            "pid": None,
            "status": "stopped",
            "lastActive": None,
            "createdAt": int(time.time()),
        }
        self.save()
        return self.workspaces[workspace_id]

    def update(self, workspace_id: str, **kwargs) -> Optional[dict]:
        if workspace_id not in self.workspaces:
            return None
        self.workspaces[workspace_id].update(kwargs)
        self.save()
        return self.workspaces[workspace_id]

    def remove(self, workspace_id: str) -> bool:
        if workspace_id in self.workspaces:
            del self.workspaces[workspace_id]
            self.save()
            return True
        return False

    def find_by_port(self, port: int) -> Optional[dict]:
        for ws in self.workspaces.values():
            if ws.get("port") == port:
                return ws
        return None

    def find_by_path(self, path: str) -> Optional[dict]:
        for ws in self.workspaces.values():
            if ws.get("path") == path:
                return ws
        return None

    def find_by_user_and_name(self, user_id: str, name: str) -> Optional[dict]:
        for ws in self.workspaces.values():
            if ws.get("user_id") == user_id and ws.get("name") == name:
                return ws
        return None


# Singleton
_registry: Optional[ProcessRegistry] = None


def get_registry() -> ProcessRegistry:
    global _registry
    if _registry is None:
        _registry = ProcessRegistry()
    return _registry
