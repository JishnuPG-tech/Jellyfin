from fastapi import APIRouter, Body, HTTPException, WebSocket, WebSocketDisconnect, Request
from pydantic import BaseModel
from backend.app.config import settings
from sessions.db import SessionStore
from core.session_manager import SessionManager
from services.workspace_process_manager import get_workspace_manager
from services.process_registry import get_registry
import asyncio
import logging
import os
import shutil

router = APIRouter()

class CreateSessionReq(BaseModel):
    user_id: int
    project: str | None = "default"

class SendInputReq(BaseModel):
    user_id: int
    text: str

class InterruptReq(BaseModel):
    user_id: int

class SendKeyReq(BaseModel):
    user_id: int
    key: str


@router.post("/sessions/new")
async def new_session(req: CreateSessionReq):
    sm = SessionManager(settings)
    try:
        sm.ensure_session(str(req.user_id), project=req.project)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "created"}


@router.get("/sessions/{user_id}/status")
async def session_status(user_id: int):
    store = SessionStore()
    info = store.get_session(user_id)
    if not info:
        raise HTTPException(status_code=404, detail="no session")
    return info


@router.post("/sessions/send")
async def send_input(req: SendInputReq):
    sm = SessionManager(settings)
    try:
        sm.send_input(str(req.user_id), req.text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "sent"}


@router.get("/sessions/{user_id}/output")
async def get_output(user_id: int, lines: int = 200):
    sm = SessionManager(settings)
    try:
        output = sm.capture_output(str(user_id), lines=lines)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"output": output}


@router.post("/sessions/interrupt")
async def interrupt_session(req: InterruptReq):
    sm = SessionManager(settings)
    try:
        sm.interrupt(str(req.user_id))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "interrupted"}


@router.post("/sessions/key")
async def send_key(req: SendKeyReq):
    sm = SessionManager(settings)
    try:
        session = sm._session_name(str(req.user_id))
        import subprocess
        subprocess.run(["tmux", "send-keys", "-t", session, req.key], check=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "key_sent"}


import httpx


@router.get("/debug/ping-telegram")
async def ping_telegram():
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get("https://api.telegram.org")
            return {"status": resp.status_code, "text": resp.text[:100]}
    except Exception as e:
        return {"error": type(e).__name__, "message": str(e)}


@router.get("/debug/ping-url")
async def ping_url(url: str = "https://www.google.com"):
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
            return {"status": resp.status_code, "text": resp.text[:100]}
    except Exception as e:
        return {"error": type(e).__name__, "message": str(e)}


@router.get("/debug/install-log")
async def get_install_log():
    paths = [
        "/data/logs/opencode-install.log",
        "/tmp/logs/opencode-install.log",
        "/tmp/opencode-install.log"
    ]
    for path in paths:
        if os.path.exists(path):
            try:
                with open(path, "r", errors="replace") as f:
                    return {"log": f.read()}
            except Exception as e:
                return {"error": f"Failed to read {path}: {str(e)}"}
    return {"error": "Installation log file not found."}


@router.get("/debug/opencode-path")
async def debug_opencode_path(directory: str = "/data/workspaces"):
    try:
        async with httpx.AsyncClient(base_url="http://127.0.0.1:4096", timeout=10.0) as c:
            import urllib.parse
            r = await c.get(f"/path?directory={urllib.parse.quote(directory, safe='/')}")
            return {
                "status": r.status_code,
                "raw": r.text[:4000],
                "json": r.json() if r.headers.get("content-type", "").startswith("application/json") else None,
                "directory": directory
            }
    except Exception as e:
        return {"error": str(e)}


@router.get("/debug/opencode-log")
async def debug_opencode_log():
    for path in ["/data/logs/opencode-serve.log", "/tmp/logs/opencode-serve.log"]:
        if os.path.exists(path):
            with open(path, "r", errors="replace") as f:
                content = f.read()
            return {"log": content[-5000:], "path": path}
    return {"error": "Log not found"}


@router.get("/debug/workspace-ls")
async def debug_workspace_ls():
    wp = os.environ.get("WORKSPACE_PATH", "/data/workspaces")
    result = {}
    try:
        result["workspace"] = wp
        result["exists"] = os.path.exists(wp)
        result["contents"] = os.listdir(wp) if os.path.exists(wp) else []
        result["home"] = os.environ.get("HOME", "N/A")
        result["home_contents"] = os.listdir(os.environ.get("HOME", "/root")) if os.path.exists(os.environ.get("HOME", "/root")) else []
    except Exception as e:
        result["error"] = str(e)
    return result


@router.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    from bot.telegram_bot import telegram_app, _handle_webhook_update
    if not telegram_app:
        return {"error": "bot not running"}
    try:
        data = await request.json()
        await _handle_webhook_update(data)
    except Exception as e:
        logging.error(f"Webhook error: {e}")
    return {"ok": True}


# ── Workspace management ──

class WorkspaceCloneReq(BaseModel):
    user_id: int
    repo_url: str
    folder_name: str | None = None

class WorkspaceDeleteReq(BaseModel):
    user_id: int
    folder_name: str

@router.get("/workspace/list")
async def list_workspaces(user_id: int):
    sm = SessionManager(settings)
    user_dir = os.path.join(sm.workspace_root, f"user_{user_id}")
    if not os.path.exists(user_dir):
        return {"folders": ["default"]}
    try:
        folders = [
            d for d in os.listdir(user_dir)
            if os.path.isdir(os.path.join(user_dir, d)) and not d.startswith(".")
        ]
        if "default" not in folders:
            folders.insert(0, "default")
        return {"folders": folders}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/workspace/clone")
async def clone_workspace(req: WorkspaceCloneReq):
    sm = SessionManager(settings)
    user_dir = os.path.join(sm.workspace_root, f"user_{req.user_id}")
    os.makedirs(user_dir, exist_ok=True)

    folder_name = req.folder_name
    if not folder_name:
        folder_name = req.repo_url.rstrip("/").split("/")[-1]
        if folder_name.endswith(".git"):
            folder_name = folder_name[:-4]

    target_path = os.path.join(user_dir, folder_name)
    if os.path.exists(target_path):
        raise HTTPException(status_code=400, detail="Folder already exists")

    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", req.repo_url, target_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise Exception(stderr.decode().strip())

        try:
            wm = get_workspace_manager()
            wm.get_or_create_workspace(str(req.user_id), folder_name, target_path)
        except Exception as reg_err:
            logging.error(f"Registry update failed: {reg_err}")

        return {"status": "cloned", "folder": folder_name}
    except Exception as e:
        logging.error(f"clone_workspace error: {e}")
        raise HTTPException(status_code=500, detail=f"Clone failed: {str(e)}")

@router.post("/workspace/delete")
async def delete_workspace(req: WorkspaceDeleteReq):
    sm = SessionManager(settings)
    target_path = os.path.join(sm.workspace_root, f"user_{req.user_id}", req.folder_name)
    if not os.path.exists(target_path):
        raise HTTPException(status_code=404, detail="Folder not found")
    if req.folder_name == "default":
        raise HTTPException(status_code=400, detail="Cannot delete default workspace")

    # Stop process if running
    wm = get_workspace_manager()
    ws_id = f"user_{req.user_id}_{req.folder_name}"
    await wm.stop(ws_id)
    wm.registry.remove(ws_id)

    try:
        shutil.rmtree(target_path)
        return {"status": "deleted"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Per-workspace process management ──

class StartWorkspaceReq(BaseModel):
    user_id: int
    folder_name: str

@router.post("/workspace/start")
async def start_workspace(req: StartWorkspaceReq):
    wm = get_workspace_manager()
    ws_id = f"user_{req.user_id}_{req.folder_name}"
    ws = wm.registry.get(ws_id)
    if not ws:
        # Auto-create registry entry if folder exists
        sm = SessionManager(settings)
        path = os.path.join(sm.workspace_root, f"user_{req.user_id}", req.folder_name)
        if not os.path.exists(path):
            raise HTTPException(status_code=404, detail="Workspace folder not found")
        ws = wm.get_or_create_workspace(str(req.user_id), req.folder_name, path)

    try:
        result = await wm.start(ws_id)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/workspace/stop")
async def stop_workspace(req: StartWorkspaceReq):
    wm = get_workspace_manager()
    ws_id = f"user_{req.user_id}_{req.folder_name}"
    # Fire and forget - don't wait for process to fully die
    asyncio.create_task(wm.stop(ws_id))
    return {"status": "stopping"}

@router.get("/workspace/status")
async def workspace_status(user_id: int):
    try:
        wm = get_workspace_manager()
        workspaces = wm.registry.get_by_user(str(user_id))
        return {
            "workspaces": [
                {
                    "id": ws["id"],
                    "name": ws["name"],
                    "status": ws.get("status", "stopped"),
                    "port": ws.get("port"),
                }
                for ws in workspaces
            ]
        }
    except Exception as e:
        logging.error(f"workspace_status error: {e}")
        return {"workspaces": [], "error": str(e)}


@router.get("/workspace/test")
async def test_workspace_manager():
    try:
        wm = get_workspace_manager()
        return {
            "registry_count": len(wm.registry.list_all()),
            "workspaces": wm.registry.list_all(),
        }
    except Exception as e:
        logging.error(f"test_workspace_manager error: {e}")
        return {"error": str(e)}


@router.websocket("/ws/session/{user_id}")
async def websocket_session(websocket: WebSocket, user_id: str, project: str = "default"):
    await websocket.accept()
    sm = SessionManager(settings)
    from bot.telegram_bot import clean_terminal_output

    sm.ensure_session(user_id, project=project)

    async def stream_to_client():
        try:
            async for chunk in sm.stream_output(user_id):
                display, is_card = clean_terminal_output(chunk, keep_whitespace=True)
                if display:
                    await websocket.send_json({
                        "type": "output",
                        "text": display,
                        "is_card": is_card
                    })
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logging.error(f"Error in stream_to_client: {e}")

    streamer_task = asyncio.create_task(stream_to_client())

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")
            if msg_type == "command":
                sm.send_input(user_id, data.get("text"))
            elif msg_type == "key":
                session = sm._session_name(user_id)
                import subprocess
                subprocess.run(["tmux", "send-keys", "-t", session, data.get("key")], check=True)
            elif msg_type == "interrupt":
                sm.interrupt(user_id)
    except WebSocketDisconnect:
        logging.info(f"WebSocket disconnected for user: {user_id}")
    except Exception as e:
        logging.error(f"WebSocket error for user {user_id}: {e}")
    finally:
        streamer_task.cancel()
        try:
            await websocket.close()
        except Exception:
            pass
