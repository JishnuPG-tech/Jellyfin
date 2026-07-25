"""Terminal-on-top-of-opencode gateway.

Routes:
  /terminal/*  -> ttyd on :7681 (internal, terminal)
  /            -> redirect HTML (auto-load latest session)
  /server/<base> (bare, no session id) -> redirect HTML
  /*           -> opencode serve on :4096 (passthrough)
"""
from __future__ import annotations

import asyncio
import os
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, Response

OC_HOST  = "127.0.0.1"
OC_PORT  = int(os.environ.get("OPENCODE_PORT", "4096"))
TTYD_PORT = int(os.environ.get("TTYD_PORT", "7681"))

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


_REDIRECT_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>OpenCode</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0d1117;color:#c9d1d9;font-family:'JetBrains Mono',Menlo,monospace;
     display:flex;align-items:center;justify-content:center;height:100vh}
.box{background:#161b22;padding:28px 36px;border:1px solid #30363d;border-radius:8px;
     max-width:560px;text-align:center}
h2{color:#58a6ff;font-size:13px;letter-spacing:.06em;text-transform:uppercase;margin:0 0 14px}
pre{color:#c9d1d9;font-size:12px;line-height:1.6;text-align:left;
    white-space:pre-wrap;word-break:break-all;background:transparent;margin:0}
a{color:#22c55e;text-decoration:none}a:hover{text-decoration:underline}
</style>
</head>
<body>
<div class="box">
  <h2>OpenCode</h2>
  <pre id="s">loading&hellip;</pre>
</div>
<script>
(async function(){
var s=document.getElementById("s"),h=location.protocol+"//"+location.host;
try{
  var r=await fetch("/api/session");
  if(!r.ok)throw new Error(r.status+" "+r.statusText);
  var d=await r.json(),arr=(d&&d.data)||[];
  if(!arr.length){s.innerHTML='no sessions yet &mdash; <a href="/server/">open dashboard</a>';return}
  var sid=arr[0].id,dest="/server/"+btoa(h)+"/session/"+encodeURIComponent(sid);
  s.innerHTML='<a href="'+dest+'">opening session&hellip;</a>';
  location.replace(dest);
}catch(e){s.innerHTML="error: "+e.message+"<br>try <a href='/server/'>/server/</a>"}
})();
</script>
</body>
</html>"""


def _is_terminal(path: str) -> bool:
    return path == "/terminal" or path.startswith("/terminal/")


def _needs_redirect(path: str) -> bool:
    if not path or path in ("/", ""):
        return True
    if path.startswith("/server"):
        parts = [p for p in path.split("/") if p]
        # /server, /server/, /server/<base>  -- no session id present
        if len(parts) < 4:
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
    timeout = httpx.Timeout(600.0, connect=10.0)

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        try:
            up = await client.request(
                method=request.method,
                url=url,
                headers=headers,
                content=body if body else None,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.HTTPError) as exc:
            return Response(status_code=502, content=f"upstream {port} unreachable: {exc}".encode())

        # Build response headers
        resp_headers = {}
        for k, v in up.headers.items():
            kl = k.lower()
            if kl in ("content-length", "content-encoding", "transfer-encoding", "connection"):
                continue
            resp_headers[k] = v

        ct = up.headers.get("content-type", "application/octet-stream")

        # For SSE / streaming: relay raw bytes
        if "text/event-stream" in ct or "chunked" in up.headers.get("transfer-encoding", ""):
            async def relay():
                try:
                    async for chunk in up.aiter_bytes():
                        yield chunk
                finally:
                    await up.aclose()
            return StreamingResponse(relay(), status_code=up.status_code, headers=resp_headers, media_type=ct)

        # Buffer entire response (most responses are small JSON or HTML)
        content = up.content
        await up.aclose()
        return Response(status_code=up.status_code, content=content, headers=resp_headers, media_type=ct)


@app.get("/_healthz", include_in_schema=False)
async def healthz():
    return JSONResponse({"status": "ok", "oc_port": OC_PORT, "ttyd_port": TTYD_PORT})


@app.api_route(
    "/{path:path}",
    methods=["GET","POST","PUT","DELETE","PATCH","HEAD","OPTIONS"],
    include_in_schema=False,
)
async def main_route(request: Request, path: str):
    full = "/" + path
    if _is_terminal(full):
        # Strip /terminal prefix when forwarding to ttyd
        fwd_path = path[len("terminal"):]  # "terminal/foo" -> "/foo"
        if not fwd_path or fwd_path[0] != "/":
            fwd_path = "/" + fwd_path
        return await _forward(request, TTYD_PORT, fwd_path)
    if _needs_redirect(full):
        return HTMLResponse(content=_REDIRECT_HTML, status_code=200)
    return await _forward(request, OC_PORT, path)


@app.websocket("/{path:path}")
async def ws_route(websocket: WebSocket, path: str):
    full = "/" + path
    target_port = TTYD_PORT if _is_terminal(full) else OC_PORT
    target_url  = f"ws://{OC_HOST}:{target_port}/{path}"
    if websocket.query_params:
        target_url += "?" + urlencode(websocket.query_params)
    import websockets as ws_lib
    await websocket.accept()
    try:
        async with ws_lib.connect(target_url, max_size=32*1024*1024) as upstream:
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
