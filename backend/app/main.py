"""Minimal everything-byte-proxy on :7860.

Three internal services:
  - opencode serve on :4096 (Chat AI server).
  - ttyd         on :7681 (embedded terminal at /terminal).

The proxy does ONLY:
  - Forward HTTP body as-is.
  - Forward HTTP headers (except `Host:`, which is rewritten to 127.0.0.1:4096
    or 127.0.0.1:7681).
  - Insert `Authorization: Basic opencode:password` for upstream = opencode.
  - Forward WebSocket frames byte-for-byte.

The proxy does NOT inspect, parse, rewrite or even read the response body.
"""
from __future__ import annotations

import asyncio
import base64
import os
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Request, WebSocket

OC_HOST = os.environ.get("OPENCODE_HOST", "127.0.0.1")
OC_PORT = int(os.environ.get("OPENCODE_PORT", "4096"))
TTYD_PORT = int(os.environ.get("TTYD_PORT", "7681"))
OC_AUTH = "Basic " + base64.b64encode(
    (os.environ.get("OPENCODE_SERVER_USERNAME", "opencode") + ":" + os.environ.get("OPENCODE_SERVER_PASSWORD", "password")).encode()
).decode()

app = FastAPI(title="opencode-serve")


def _target_port(path: str) -> int:
    if path == "/terminal" or path.startswith("/terminal/") or path == "/terminal/" or path == "/terminal":
        return TTYD_PORT
    return OC_PORT


async def _proxy(request: Request):
    target_port = _target_port(request.url.path)
    # httpx will percent-encode path; but we pass the literal path as URL.
    upstream_url = f"http://{OC_HOST}:{target_port}{request.url.path}"
    fwd = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}
    if target_port == OC_PORT:
        fwd["authorization"] = OC_AUTH
    body = await request.body()

    async with httpx.AsyncClient(timeout=600.0, follow_redirects=False) as cli:
        try:
            upstream = await cli.request(
                method=request.method,
                url=upstream_url,
                headers=fwd,
                params=[(k, v) for k, v in request.query_params.multi_items()],
                content=body,
            )
        except (httpx.ConnectError, httpx.HTTPError) as exc:
            from fastapi.responses import Response
            return Response(status_code=502, content=f"upstream error: {exc}")

        passthrough = {k: v for k, v in upstream.headers.items()
                       if k.lower() not in ("content-length", "content-encoding", "transfer-encoding", "connection")}

        async def relay():
            try:
                async for ch in upstream.aiter_raw():
                    if ch:
                        yield ch
            finally:
                try:
                    await upstream.aclose()
                except Exception:
                    pass

        from fastapi.responses import StreamingResponse
        return StreamingResponse(
            relay(),
            status_code=upstream.status_code,
            headers=passthrough,
            media_type=upstream.headers.get("content-type") or "application/octet-stream",
        )


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def serve(request: Request, path: str):
    return await _proxy(request)


@app.api_route("/", methods=["GET"], include_in_schema=False)
async def serve_root(request: Request):
    return await _proxy(request)


@app.websocket("/{path:path}")
async def ws_serve(websocket: WebSocket, path: str):
    await websocket.accept()
    target_port = _target_port("/" + path)
    target_url = f"ws://{OC_HOST}:{target_port}/{path}"
    if websocket.query_params:
        target_url += "?" + urlencode(websocket.query_params)
    headers = {}
    if target_port == OC_PORT:
        headers["Authorization"] = OC_AUTH
    import websockets as ws_lib
    try:
        async with ws_lib.connect(target_url, additional_headers=headers, max_size=32 * 1024 * 1024) as upstream:
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
            t = msg.get("type")
            if t == "websocket.disconnect":
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
    return JSONResponse({"status": "ok"})
