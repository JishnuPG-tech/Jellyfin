"""FastAPI app for OpenCode-Serve.

Run by `uvicorn backend.app.main:app --host 0.0.0.0 --port 7860`.

Responsibilities
----------------
* Reverse-proxy the embedded `opencode serve` (port 4096) so:
    - The original SPA at /<session-url> still works.
    - All API endpoints (/api/*, /global/*, /events, …) still work.
    - WebSocket connections to opencode are still proxied.
* Host the **embedded terminal**:
    - Persistent `/bin/bash -i` PTY in this same container (see
      `pty_service.py`).
    - Cross-streamed through `/terminal/ws` to any browser xterm.js.
* Serve the integrated webapp at `/` — a small SPA with two tabs:
    - `Terminal` — points the user at `/terminal` (xterm.js page) OR
      embeds it in an iframe.
    - `Chat`     — points the user at the opencode session URL (proxied).
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from backend.app.opencode_proxy import AUTH_HEADER, OPENCODE_PORT, clear_host_header
from backend.app.pty_service import get_pty_service
from backend.app.webapp import WEBAPP_HTML


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
local_log = logging.getLogger("opencode-serve.main")


# ─────────────────────────────────────────────────────────────────────
# Lifespan
# ─────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    pty = get_pty_service()
    try:
        await pty.start()
        local_log.info("PTY service ready on startup")
    except Exception:
        local_log.exception("PTY service failed to start at boot")
    yield
    await pty.stop()


app = FastAPI(title="OpenCode Serve", lifespan=lifespan)


# ─────────────────────────────────────────────────────────────────────
# Health & root
# ─────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    pty = get_pty_service()
    upstream_ok = False
    try:
        import httpx
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"http://127.0.0.1:{OPENCODE_PORT}/global/health")
            upstream_ok = r.status_code == 200
    except Exception:
        upstream_ok = False
    return {
        "status": "ok",
        "pty_alive": pty.is_alive(),
        "opencode": "up" if upstream_ok else "down",
    }


@app.get("/", response_class=HTMLResponse)
async def root():
    """Integrated webapp — shows the terminal iframe + a button to the chat SPA."""
    return WEBAPP_HTML


# ─────────────────────────────────────────────────────────────────────
# Terminal API
# ─────────────────────────────────────────────────────────────────────
@app.websocket("/terminal/ws")
async def terminal_ws(client_ws: WebSocket):
    """Full-duplex terminal bridge.

    Protocol (binary-first, with a small text side-channel for control):
      * client → server **binary** frames  → raw bytes written to PTY
      * client → server **text**  frames  → either raw PTY bytes (lenient
        mode) or control frames:
          ``"resize:<cols>:<rows>"``   — change the kernel TTY size
          ``"ping"``                   — keep-alive (server replies "pong")
      * server → client **binary** frames  → raw PTY output bytes
      * server → client **text**  frames  → JSON-encoded control
                                            (``{"type":"exit","code":n}``)
    """
    await client_ws.accept()
    pty = get_pty_service()
    if not pty.is_alive():
        try:
            await pty.start()
        except Exception:
            await client_ws.close(code=1011, reason="PTY unavailable")
            return

    # Send the scrollback snapshot for fast context restore.
    snap = pty.snapshot(16_384)
    if snap:
        try:
            await client_ws.send_bytes(snap)
        except Exception:
            return

    sub = await pty.stream()

    async def reader():
        try:
            while True:
                data = await sub.get()
                if client_ws.client_state.name != "CONNECTED":
                    break
                try:
                    await client_ws.send_bytes(data)
                except Exception:
                    break
        except asyncio.CancelledError:
            pass
        except Exception:
            local_log.exception("terminal_ws reader crashed")

    reader_task = asyncio.create_task(reader())

    try:
        while True:
            msg = await client_ws.receive()
            t = msg.get("type")
            if t == "websocket.disconnect":
                break
            if "bytes" in msg and msg["bytes"] is not None:
                try:
                    await pty.write(bytes(msg["bytes"]))
                except Exception:
                    local_log.exception("PTY write failed")
            elif "text" in msg and msg["text"] is not None:
                txt = msg["text"]
                if txt.startswith("resize:"):
                    try:
                        _, cols, rows = txt.split(":")
                        await pty.resize(int(cols), int(rows))
                    except Exception:
                        pass
                elif txt == "ping":
                    await client_ws.send_text("pong")
                elif txt.startswith("__TYPE__"):
                    # Convenience: ``__TYPE__<literal>\n``  → write literal to PTY.
                    await pty.write(txt[len("__TYPE__"):].encode("utf-8"))
                else:
                    # Plain text fallback (some browsers always send text)
                    try:
                        await pty.write(txt.encode("utf-8"))
                    except Exception:
                        pass
    except Exception:
        local_log.exception("terminal_ws loop crashed")
    finally:
        reader_task.cancel()
        try:
            await reader_task
        except (asyncio.CancelledError, Exception):
            pass
        pty.unsubscribe(sub)


@app.get("/terminal/snapshot")
async def terminal_snapshot():
    """Return the most recent scrollback bytes as plain text for debugging."""
    pty = get_pty_service()
    return Response_with_text(pty.snapshot(8192))


from fastapi import Response as _Resp


def Response_with_text(data: bytes) -> _Resp:
    return _Resp(content=data, media_type="text/plain; charset=utf-8")


# ─────────────────────────────────────────────────────────────────────
# Opencode reverse proxy (HTTP + WebSocket)
# ─────────────────────────────────────────────────────────────────────
async def _opencode_proxy(request: Request, path: str, timeout: float):
    from backend.app.opencode_proxy import proxy_http
    headers = clear_host_header(dict(request.headers))
    body = await request.body()
    url = f"http://127.0.0.1:{OPENCODE_PORT}/{path}"
    if request.url.query:
        url += f"?{request.url.query}"

    import httpx
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        try:
            upstream = await client.request(
                method=request.method,
                url=url,
                headers=headers,
                content=body,
            )
        except httpx.ConnectError:
            raise HTTPException(status_code=502, detail="opencode not reachable")

        content_type = upstream.headers.get("content-type", "application/octet-stream")
        out_headers = dict(upstream.headers)
        out_headers.pop("content-length", None)

        if "text/event-stream" in content_type or upstream.headers.get("transfer-encoding"):
            async def it():
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            return StreamingResponse(it(), status_code=upstream.status_code, headers=out_headers, media_type=content_type)
        return StreamingResponse(
            iter([upstream.content]),
            status_code=upstream.status_code,
            headers=out_headers,
            media_type=content_type,
        )


@app.api_route("/global/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def proxy_global(request: Request, path: str):
    return await _opencode_proxy(request, f"global/{path}", timeout=30.0)


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def proxy_api(request: Request, path: str):
    return await _opencode_proxy(request, f"api/{path}", timeout=30.0)


@app.api_route("/server/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def proxy_server(request: Request, full_path: str):
    return await _opencode_proxy(request, f"server/{full_path}", timeout=60.0)


@app.api_route("/assets/{path:path}", methods=["GET", "OPTIONS", "HEAD"])
async def proxy_assets(request: Request, path: str):
    return await _opencode_proxy(request, f"assets/{path}", timeout=15.0)


@app.api_route("/auth/{path:path}", methods=["GET", "POST", "OPTIONS", "HEAD"])
async def proxy_auth(request: Request, path: str):
    return await _opencode_proxy(request, f"auth/{path}", timeout=15.0)


@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def proxy_v1(request: Request, path: str):
    return await _opencode_proxy(request, f"v1/{path}", timeout=60.0)


# ── Catch-all GET so static SPA assets still work (favicon, etc.) ──
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def proxy_catch_all(request: Request, path: str):
    if path == "health":
        return await health()
    if path.startswith("terminal"):
        raise HTTPException(status_code=404, detail="not found")
    return await _opencode_proxy(request, path, timeout=60.0)


# ─────────────────────────────────────────────────────────────────────
# WebSocket reverse proxy to opencode serve
# ─────────────────────────────────────────────────────────────────────
@app.websocket("/{path:path}")
async def ws_catch_all(websocket: WebSocket, path: str):
    if path.startswith("terminal"):
        await websocket.close(code=1008)
        return
    if path.startswith("ws/") or path.startswith("__"):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    import websockets

    uri = f"ws://127.0.0.1:{OPENCODE_PORT}/{path}"
    if websocket.query_params:
        qs = websocket.query_params
        uri += "?" + "&".join(f"{k}={v}" for k, v in qs.items())

    try:
        async with websockets.connect(uri, additional_headers={"Authorization": AUTH_HEADER}) as upstream:
            async def c2s():
                try:
                    while True:
                        msg = await websocket.receive()
                        if msg.get("type") == "websocket.disconnect":
                            break
                        if "text" in msg and msg["text"] is not None:
                            await upstream.send(msg["text"])
                        elif "bytes" in msg and msg["bytes"] is not None:
                            await upstream.send(msg["bytes"])
                except Exception:
                    pass

            async def s2c():
                try:
                    async for out in upstream:
                        if isinstance(out, str):
                            await websocket.send_text(out)
                        else:
                            await websocket.send_bytes(out)
                except Exception:
                    pass

            await asyncio.gather(c2s(), s2c())
    except Exception:
        try:
            await websocket.close()
        except Exception:
            pass
