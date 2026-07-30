"""
Hermes Vapi Bridge  v2.0.0
===========================
Lightweight OpenAI-compatible endpoint that calls the configured LLM **directly**
(no AIAgent overhead).  Startup: ~0.2 s.  Per-request latency: 1-5 s.

Endpoints:
  GET  /                       → Health check
  GET  /logs                   → Last 500 log lines
  POST /v1/chat/completions    → Vapi Custom LLM (OpenAI-compatible)
  WS   /ws/chat/{session_id}  → WebSocket streaming chat
"""

import asyncio
import json
import logging
import os
import threading
import time

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse


# ── In-memory log handler for /logs diagnostics ────────────────────────────────
class _MemHandler(logging.Handler):
    def __init__(self, maxlines: int = 500):
        super().__init__()
        self.logs: list[str] = []
        self._max = maxlines

    def emit(self, record):
        try:
            self.logs.append(self.format(record))
            if len(self.logs) > self._max:
                self.logs.pop(0)
        except Exception:
            pass


_mem_handler = _MemHandler()
_mem_handler.setFormatter(
    logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
)
logging.getLogger().addHandler(_mem_handler)
logger = logging.getLogger("vapi_bridge")
logger.addHandler(_mem_handler)

# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Hermes Vapi Bridge", version="2.0.0")

# ── Model config (lazy-loaded once at first request) ──────────────────────────
_cfg: dict = {}
_cfg_lock = threading.Lock()
_cfg_loaded = False

SYSTEM_PROMPT = (
    "You are Hermes, a helpful AI voice assistant. "
    "Keep responses concise, natural, and conversational — no markdown or bullet points."
)


def _load_config() -> dict:
    """Read ~/.hermes/config.yaml; fall back to env-var defaults."""
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        mc = cfg.get("model", {}) or {}
    except Exception as exc:
        logger.warning("config.yaml unavailable (%s) — using env var fallbacks", exc)
        mc = {}

    provider = os.getenv("VAPI_PROVIDER") or mc.get("provider") or "copilot"
    model    = os.getenv("VAPI_MODEL")    or mc.get("default")   or "claude-haiku-4.5"
    api_mode = os.getenv("VAPI_API_MODE") or mc.get("api_mode")  or "anthropic_messages"
    base_url = mc.get("base_url") or "https://api.githubcopilot.com"

    if provider == "copilot":
        api_key = (
            os.getenv("COPILOT_GITHUB_TOKEN")
            or os.getenv("GH_TOKEN")
            or os.getenv("GITHUB_TOKEN")
            or ""
        )
    elif provider == "openrouter":
        api_key  = os.getenv("OPENROUTER_API_KEY") or ""
        base_url = mc.get("base_url") or "https://openrouter.ai/api/v1"
    else:
        api_key = (
            os.getenv("API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or os.getenv("GITHUB_TOKEN")
            or ""
        )

    return dict(
        provider=provider, model=model,
        api_mode=api_mode, base_url=base_url, api_key=api_key,
    )


def get_cfg() -> dict:
    global _cfg, _cfg_loaded
    with _cfg_lock:
        if not _cfg_loaded:
            _cfg = _load_config()
            _cfg_loaded = True
            logger.info(
                "LLM config: provider=%s model=%s api_mode=%s key=%s",
                _cfg["provider"], _cfg["model"], _cfg["api_mode"],
                "***" if _cfg["api_key"] else "MISSING",
            )
    return _cfg


# ── Session conversation history ───────────────────────────────────────────────
_sessions: dict[str, dict] = {}
_SESSION_TTL = 600   # seconds


def _prune_sessions():
    now = time.time()
    for k in [k for k, v in _sessions.items() if now - v["ts"] > _SESSION_TTL]:
        del _sessions[k]


def _history(sid: str) -> list:
    _prune_sessions()
    if sid not in _sessions:
        _sessions[sid] = {"hist": [], "ts": time.time()}
    _sessions[sid]["ts"] = time.time()
    return _sessions[sid]["hist"]


def _push(sid: str, role: str, content: str):
    h = _history(sid)
    h.append({"role": role, "content": content})
    if len(h) > 20:                        # keep last 10 turns
        _sessions[sid]["hist"] = h[-20:]


# ── Direct LLM call ───────────────────────────────────────────────────────────
def _call_llm(messages: list, cfg: dict) -> str:
    """Call the configured LLM and return the assistant reply."""
    if cfg["api_mode"] == "anthropic_messages":
        import anthropic
        system_txt = SYSTEM_PROMPT
        conv = []
        for m in messages:
            if m["role"] == "system":
                system_txt = m["content"]
            else:
                conv.append({"role": m["role"], "content": m["content"]})
        client = anthropic.Anthropic(api_key=cfg["api_key"], base_url=cfg["base_url"])
        resp = client.messages.create(
            model=cfg["model"], max_tokens=512,
            system=system_txt, messages=conv,
        )
        return resp.content[0].text
    else:
        from openai import OpenAI
        client = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"])
        resp = client.chat.completions.create(
            model=cfg["model"], messages=messages, max_tokens=512,
        )
        return resp.choices[0].message.content


def _chat(sid: str, user_msg: str) -> str:
    """One conversational turn: call LLM, save history, return reply."""
    cfg = get_cfg()
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    msgs.extend(_history(sid))
    msgs.append({"role": "user", "content": user_msg})

    t0 = time.time()
    reply = _call_llm(msgs, cfg)
    logger.info("LLM %.2fs | session=%s | reply=%s…", time.time() - t0, sid, reply[:60])

    _push(sid, "user", user_msg)
    _push(sid, "assistant", reply)
    return reply


# ── Startup ────────────────────────────────────────────────────────────────────
@app.on_event("startup")
def _startup():
    # Resolve config eagerly in background so first request is instant
    threading.Thread(target=get_cfg, daemon=True).start()


# ── Health ─────────────────────────────────────────────────────────────────────
@app.get("/")
async def health():
    cfg = get_cfg()
    return JSONResponse({
        "status": "ok",
        "service": "Hermes Vapi Bridge",
        "version": "2.0.0",
        "provider": cfg["provider"],
        "model": cfg["model"],
        "active_sessions": len(_sessions),
        "vapi_endpoint": "/v1/chat/completions",
    })


@app.get("/logs")
async def get_logs():
    return PlainTextResponse("\n".join(_mem_handler.logs))


# ── WebSocket streaming ────────────────────────────────────────────────────────
@app.websocket("/ws/chat/{session_id}")
async def ws_chat(websocket: WebSocket, session_id: str):
    await websocket.accept()
    logger.info("WS connected: %s", session_id)
    loop = asyncio.get_event_loop()
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except Exception:
                continue

            if data.get("type") in ("ping", "pong"):
                continue

            user_msg = data.get("content", "").strip()
            if not user_msg:
                continue

            logger.info("WS %s ← %s", session_id, user_msg[:80])
            try:
                reply = await loop.run_in_executor(None, _chat, session_id, user_msg)
                await websocket.send_json({"type": "token", "content": reply})
            except Exception as exc:
                logger.error("WS %s error: %s", session_id, exc)
                await websocket.send_json({"type": "error", "content": str(exc)})
            finally:
                await websocket.send_json({"type": "done"})
    except WebSocketDisconnect:
        logger.info("WS disconnected: %s", session_id)
    except Exception as exc:
        logger.error("WS fatal %s: %s", session_id, exc)


# ── Vapi HTTP endpoint ─────────────────────────────────────────────────────────
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    messages   = body.get("messages", [])
    do_stream  = body.get("stream", False)

    call_id = (
        request.headers.get("x-vapi-call-id")
        or request.headers.get("X-Vapi-Call-Id")
        or (body.get("call") or {}).get("id", "default")
    )

    # Extract the latest user message
    user_msg = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            c = msg.get("content", "")
            user_msg = (
                " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                if isinstance(c, list) else str(c)
            )
            break

    if not user_msg.strip():
        raise HTTPException(status_code=400, detail="No user message found")

    logger.info("Call %s ← %s", call_id, user_msg[:100])

    loop = asyncio.get_event_loop()
    try:
        reply = await loop.run_in_executor(None, _chat, call_id, user_msg)
    except Exception as exc:
        logger.error("LLM error call=%s: %s", call_id, exc)
        reply = "Sorry, I encountered an error processing your request."

    logger.info("Call %s → %s", call_id, reply[:100])

    cid = f"chatcmpl-{call_id}-{int(time.time())}"

    if do_stream:
        async def _stream():
            yield f"data: {json.dumps({'id': cid, 'object': 'chat.completion.chunk', 'model': 'hermes', 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': reply}, 'finish_reason': None}]})}\n\n"
            yield f"data: {json.dumps({'id': cid, 'object': 'chat.completion.chunk', 'model': 'hermes', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            _stream(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return JSONResponse({
        "id": cid, "object": "chat.completion", "model": "hermes",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": reply},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    })
