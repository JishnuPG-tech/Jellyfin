"""Absolute-minimum multi-protocol gateway.

A 50-line uvicorn app whose only job is:
  - bind :7860
  - mint an OPENCODE auth header for the proxy
  - forward every HTTP request to upstream
  - forward every WebSocket to upstream

We never inspect, rewrite, buffer, or modify the body — only headers
going upstream and response status/headers being passed through.
"""
from __future__ import annotations

import asyncio
import base64
import os
import time
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Request, WebSocket

OC_HOST = os.environ.get("OPENCODE_HOST", "127.0.0.1")
OC_PORT = int(os.environ.get("OPENCODE_PORT", "4096"))
TTYD_PORT = int(os.environ.get("TTYD_PORT", "7681"))
OC_AUTH = "Basic " + base64.b64encode(
    (os.environ.get("OPENCODE_SERVER_USERNAME", "opencode") + ":" + os.environ.get("OPENCODE_SERVER_PASSWORD", "password")).encode()
).decode()

START = time.time()
app = FastAPI(title="opencode-serve")


def pick_upstream_port(path: str) -> int:
    if path.startswith("/terminal"):
        return TTYD_PORT
    return OC_PORT


async def _proxy_http(request: Request):
    upstream_port = pick_upstream_port(request.url.path)
    upstream_url = f"http://{OC_HOST}:{upstream_port}{request.url.path}"
    if request.url.query:
        upstream_url += "?" + str(request.url.query)
    fwd_headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}
    if upstream_port == OC_PORT:
        fwd_headers["authorization"] = OC_AUTH
    body = await request.body()

    async with httpx.AsyncClient(timeout=600.0, follow_redirects=False) as cli:
        try:
            upstream = await cli.request(
                method=request.method,
                url=upstream_url,
                headers=fwd_headers,
                content=body,
            )
        except (httpx.ConnectError, httpx.HTTPError) as e:
            from fastapi.responses import Response
            return Response(status_code=502, content=f"upstream {upstream_port} unreachable: {e}")

        out_headers = {k: v for k, v in upstream.headers.items()
                       if k.lower() not in ("content-length", "content-encoding", "transfer-encoding", "connection")}

        async def relay():
            async for chunk in upstream.aiter_raw():
                if chunk:
                    yield chunk
            await upstream.aclose()

        from fastapi.responses import StreamingResponse
        return StreamingResponse(
            relay(),
            status_code=upstream.status_code,
            headers=out_headers,
            media_type=upstream.headers.get("content-type") or "application/octet-stream",
        )


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def http_route(request: Request, path: str):
    return await _proxy_http(request)


@app.websocket("/{path:path}")
async def ws_route(websocket: WebSocket, path: str):
    await websocket.accept()
    import websockets as ws_lib
    upstream_port = pick_upstream_port("/" + path)
    upstream_url = f"ws://{OC_HOST}:{upstream_port}/{path}"
    if websocket.query_params:
        upstream_url += "?" + urlencode(websocket.query_params)
    headers = {"Authorization": OC_AUTH} if upstream_port == OC_PORT else {}
    try:
        async with ws_lib.connect(upstream_url, additional_headers=headers, max_size=None) as upstream:
            await asyncio.gather(_c2s(websocket, upstream), _s2c(websocket, upstream))
    except Exception:
        try:
            await websocket.close()
        except Exception:
            pass


async def _c2s(c, up):
    try:
        while True:
            msg = await c.receive()
            if msg.get("type") == "websocket.disconnect":
                return
            if msg.get("text") is not None:
                await up.send(msg["text"])
            elif msg.get("bytes") is not None:
                await up.send(msg["bytes"])
    except Exception:
        return


async def _s2c(c, up):
    try:
        async for raw in up:
            if isinstance(raw, str):
                await c.send_text(raw)
            else:
                await c.send_bytes(raw)
    except Exception:
        return


@app.get("/healthz", include_in_schema=False)
async def healthz():
    from fastapi.responses import JSONResponse
    return JSONResponse({"status": "ok", "uptime_s": round(time.time() - START, 1)})
