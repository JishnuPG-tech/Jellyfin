"""Byte-proxy that adds ONLY /terminal/* routing on top of an opencode
serve that already binds 7860.

Architecture:
  - opencode serve listens on :7860 (HF exposed).
  - This uvicorn process listens on port TTYN_PORT (internal, e.g. 7680).
  - uvicorn forwards /terminal/* to ttyd on 7681, /proxy/* to itself
    expects nothing — actually, this stance is wrong. Re-read.

Final architecture:
  We bind uvicorn on 7860 (HF port). uvicorn proxies ALL paths to
  opencode serve on a private port (4096). /terminal/* goes to ttyd
  on 7681. This is the ONLY realistic topology with one HF port.

The proxy IS a dumb pipe: it does NOT touch the body, does NOT log,
does NOT add JSON wrappers. The only mutation is adding the opencode
HTTP-Basic auth header.
"""
from __future__ import annotations

import asyncio
import base64
import os
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Request, WebSocket

OC_HOST = "127.0.0.1"
OC_PORT = int(os.environ.get("OPENCODE_PORT", "4096"))
TTYD_PORT = int(os.environ.get("TTYD_PORT", "7681"))
OC_USERNAME = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
OC_PASSWORD = os.environ.get("OPENCODE_SERVER_PASSWORD", "password")
OC_AUTH = "Basic " + base64.b64encode(f"{OC_USERNAME}:{OC_PASSWORD}".encode()).decode()

# Timeouts
REQ_TIMEOUT = 600.0  # long enough for SSE

app = FastAPI(title="opencode-serve", docs_url=None, redoc_url=None, openapi_url=None)


def _target_for(path: str) -> tuple[str, int, dict[str, str]]:
    """Return (target_label, target_port, extra_headers)."""
    if path == "/terminal" or path.startswith("/terminal/") or path.startswith("/terminal?"):
        return "ttyd", TTYD_PORT, {}
    return "opencode", OC_PORT, {"Authorization": OC_AUTH}


async def _proxy_http(request: Request):
    target_label, target_port, extra_headers = _target_for(request.url.path)
    upstream_url = f"http://{OC_HOST}:{target_port}{request.url.path}"
    fwd = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}
    fwd.update(extra_headers)
    body = await request.body()
    async with httpx.AsyncClient(timeout=REQ_TIMEOUT, follow_redirects=False) as cli:
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
            return Response(status_code=502, content=f"{target_label} unreachable: {exc}")

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
async def main_proxy(request: Request, path: str):
    return await _proxy_http(request)


@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
async def root_proxy(request: Request):
    return await _proxy_http(request)


@app.websocket("/{path:path}")
async def ws_proxy(websocket: WebSocket, path: str):
    await websocket.accept()
    target_label, target_port, extra_headers = _target_for("/" + path)
    target_url = f"ws://{OC_HOST}:{target_port}/{path}"
    if websocket.query_params:
        target_url += "?" + urlencode(websocket.query_params)

    import websockets as ws_lib
    try:
        async with ws_lib.connect(target_url, additional_headers=extra_headers, max_size=32 * 1024 * 1024) as upstream:
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
    return JSONResponse({"status": "ok", "oc_port": OC_PORT, "ttyd_port": TTYD_PORT})
