from fastapi import APIRouter, Body, HTTPException, WebSocket, WebSocketDisconnect, Request
from pydantic import BaseModel
from backend.app.config import settings
from sessions.db import SessionStore
from core.session_manager import SessionManager
import asyncio
import logging
import os

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


@router.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    """Receive Telegram updates via webhook (for HuggingFace Spaces / environments without outbound access)."""
    from bot.telegram_bot import telegram_app, _handle_webhook_update
    if not telegram_app:
        return {"error": "bot not running"}
    try:
        data = await request.json()
        await _handle_webhook_update(data)
    except Exception as e:
        logging.error(f"Webhook error: {e}")
    return {"ok": True}


@router.websocket("/ws/session/{user_id}")
async def websocket_session(websocket: WebSocket, user_id: str):
    await websocket.accept()
    sm = SessionManager(settings)
    from bot.telegram_bot import clean_terminal_output
    
    sm.ensure_session(user_id)
    
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
