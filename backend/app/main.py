"""OpenCode-Serve gateway.

Routes:
  /assets/*     -> frontend static files
  /terminal/*   -> ttyd on :7681 (internal)
  /             -> frontend SPA index.html
  /favicon-*.png, /apple-touch-*.png, /site.webmanifest -> frontend assets
  /*            -> opencode serve on :4096 (passthrough)
"""
from __future__ import annotations

import asyncio
import mimetypes
import os
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

OC_HOST = "127.0.0.1"
OC_PORT = int(os.environ.get("OPENCODE_PORT", "4096"))
TTYD_PORT = int(os.environ.get("TTYD_PORT", "7681"))

FRONTEND_DIR = Path("/app/frontend")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def _read_frontend(name: str) -> bytes | None:
    p = FRONTEND_DIR / name
    if p.exists() and p.is_file():
        return p.read_bytes()
    return None


_INDEX_HTML = _read_frontend("index.html") or b"<h1>frontend not found</h1>"


def _is_terminal(path: str) -> bool:
    return path == "/terminal" or path.startswith("/terminal/")


def _is_frontend_asset(path: str) -> bool:
    if path.startswith("assets/"):
        return True
    if path in ("favicon-96x96-v3.png", "favicon-v3.svg", "favicon-v3.ico",
                "apple-touch-icon-v3.png", "site.webmanifest", "social-share.png"):
        return True
    return False


def _needs_redirect(path: str) -> bool:
    if not path or path in ("/", ""):
        return True
    return False


async def _forward(request: Request, port: int, path: str):
    url = f"http://{OC_HOST}:{port}/{path}"
    if request.url.query:
        url += "?" + request.url.query

    headers = {}
    for k, v in request.headers.items():
        kl = k.lower()
        if kl in ("host", "content-length", "transfer-encoding"):
            continue
        headers[k] = v

    body = await request.body()

    client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0), follow_redirects=False)
    try:
        up = await client.send(
            client.build_request(method=request.method, url=url, headers=headers, content=body if body else None),
            stream=True,
        )
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.HTTPError) as exc:
        await client.aclose()
        return Response(status_code=502, content=f"upstream {port} unreachable: {exc}".encode())

    resp_headers = {}
    for k, v in up.headers.items():
        kl = k.lower()
        if kl in ("content-length", "content-encoding", "transfer-encoding", "connection"):
            continue
        resp_headers[k] = v

    ct = up.headers.get("content-type", "application/octet-stream")

    async def relay():
        try:
            async for chunk in up.aiter_bytes():
                if chunk:
                    yield chunk
        finally:
            try:
                await up.aclose()
            except Exception:
                pass
            try:
                await client.aclose()
            except Exception:
                pass

    return StreamingResponse(relay(), status_code=up.status_code, headers=resp_headers, media_type=ct)


@app.get("/_healthz", include_in_schema=False)
async def healthz():
    return JSONResponse({"status": "ok", "oc_port": OC_PORT, "ttyd_port": TTYD_PORT})


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def main_route(request: Request, path: str):
    full = "/" + path

    # Terminal proxy
    if _is_terminal(full):
        fwd_path = path[len("terminal"):]
        if not fwd_path or fwd_path[0] != "/":
            fwd_path = "/" + fwd_path
        return await _forward(request, TTYD_PORT, fwd_path)

    # Frontend assets
    if _is_frontend_asset(path):
        name = path.split("/", 1)[-1] if "/" in path else path
        data = _read_frontend(name)
        if data is not None:
            ct, _ = mimetypes.guess_type(name)
            return Response(content=data, media_type=ct or "application/octet-stream")

    # Frontend SPA: serve index.html for / or /server/ paths
    if _needs_redirect(full) or (path.startswith("server") and not any(
        seg in path.split("/") for seg in ("session", "message", "event", "prompt")
    )):
        return Response(content=_INDEX_HTML, media_type="text/html")

    # Everything else -> opencode upstream
    return await _forward(request, OC_PORT, path)


@app.websocket("/{path:path}")
async def ws_route(websocket: WebSocket, path: str):
    full = "/" + path
    target_port = TTYD_PORT if _is_terminal(full) else OC_PORT
    target_url = f"ws://{OC_HOST}:{target_port}/{path}"
    if websocket.query_params:
        target_url += "?" + urlencode(websocket.query_params)

    import websockets as ws_lib

    await websocket.accept()
    try:
        async with ws_lib.connect(target_url, max_size=32 * 1024 * 1024) as upstream:

            async def c2s():
                try:
                    while True:
                        msg = await websocket.receive()
                        if msg.get("type") == "websocket.disconnect":
                            return
                        if msg.get("text") is not None:
                            await upstream.send(msg["text"])
                        elif msg.get("bytes") is not None:
                            await upstream.send(msg["bytes"])
                except Exception:
                    return

            async def s2c():
                try:
                    async for raw in upstream:
                        if isinstance(raw, str):
                            await websocket.send_text(raw)
                        else:
                            await websocket.send_bytes(raw)
                except Exception:
                    return

            await asyncio.gather(c2s(), s2c())
    except Exception:
        try:
            await websocket.close()
        except Exception:
            pass
