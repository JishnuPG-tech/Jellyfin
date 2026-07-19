import os
import json
import base64
from contextlib import asynccontextmanager
from backend.app.handlers import healthz
from bot.telegram_bot import run_bot_async, stop_bot_async


@asynccontextmanager
async def lifespan(app):
    import asyncio
    bot_task = asyncio.create_task(run_bot_async())
    yield
    await stop_bot_async()
    bot_task.cancel()
    try:
        await bot_task
    except asyncio.CancelledError:
        pass


try:
    import httpx
    import asyncio
    import websockets
    from fastapi import FastAPI, Request, HTTPException, WebSocket
    from fastapi.responses import StreamingResponse, JSONResponse
    from backend.app.api import router as api_router

    # ── Auth credentials for opencode serve ──────────────────────────
    _OC_USERNAME = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
    _OC_PASSWORD = os.environ.get("OPENCODE_SERVER_PASSWORD", "password")
    _OC_AUTH_B64 = base64.b64encode(f"{_OC_USERNAME}:{_OC_PASSWORD}".encode()).decode()
    _OC_AUTH_HEADER = f"Basic {_OC_AUTH_B64}"

    WORKSPACE_PATH = os.environ.get("WORKSPACE_PATH", "/data/workspaces")

    # ── Global persistent HTTP client ────────────────────────────────
    http_client = httpx.AsyncClient(
        base_url="http://127.0.0.1:4096",
        timeout=120.0,
        headers={"Authorization": _OC_AUTH_HEADER},
    )

    app = FastAPI(title="Opencode Bridge", lifespan=lifespan)
    app.include_router(api_router, prefix="/api")

    # ── Folder helpers ───────────────────────────────────────────────
    def list_workspace_folders(base_path: str = None):
        """Return all directories under the workspace as opencode path entries."""
        base_path = base_path or WORKSPACE_PATH
        entries = []
        try:
            for entry in sorted(os.scandir(base_path), key=lambda e: e.name):
                if not entry.name.startswith("."):
                    entries.append({
                        "name": entry.name,
                        "path": entry.path,
                        "type": "directory" if entry.is_dir(follow_symlinks=True) else "file",
                    })
        except Exception:
            pass
        return entries

    def make_path_response(directory: str):
        """Build response matching opencode's /path endpoint format."""
        entries = list_workspace_folders(directory)
        return {
            "path": directory,
            "home": os.environ.get("HOME", "/data"),
            "root": directory in ("/", ""),
            "entries": entries,
        }

    # ── WebSocket reverse proxy ──────────────────────────────────────
    @app.websocket("/{path:path}")
    async def websocket_proxy(client_ws: WebSocket, path: str):
        await client_ws.accept()
        uri = f"ws://127.0.0.1:4096/{path}"
        if client_ws.query_params:
            uri += f"?{client_ws.query_params}"

        extra_headers = {"Authorization": _OC_AUTH_HEADER}
        try:
            async with websockets.connect(uri, extra_headers=extra_headers) as server_ws:
                async def client_to_server():
                    try:
                        while True:
                            msg = await client_ws.receive()
                            if "text" in msg:
                                await server_ws.send(msg["text"])
                            elif "bytes" in msg:
                                await server_ws.send(msg["bytes"])
                    except Exception:
                        pass

                async def server_to_client():
                    try:
                        async for msg in server_ws:
                            if isinstance(msg, str):
                                await client_ws.send_text(msg)
                            else:
                                await client_ws.send_bytes(msg)
                    except Exception:
                        pass

                await asyncio.gather(client_to_server(), server_to_client())
        except Exception:
            pass

    # ── HTTP reverse proxy with folder interception ──────────────────
    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
    async def proxy_request(request: Request, path: str):
        if request.headers.get("upgrade", "").lower() == "websocket":
            raise HTTPException(status_code=400, detail="WebSocket upgrade must use ws:// endpoint")

        # ── Intercept /find/file — inject real workspace folders ──────
        if path == "find/file":
            params = dict(request.query_params)
            query = params.get("query", "").lower()
            # Forward to opencode first
            try:
                oc_url = "/find/file"
                if request.url.query:
                    oc_url += f"?{request.url.query}"
                fwd_headers = dict(request.headers)
                fwd_headers.pop("host", None)
                fwd_headers["authorization"] = _OC_AUTH_HEADER
                oc_req = http_client.build_request("GET", oc_url, headers=fwd_headers)
                oc_r = await http_client.send(oc_req)
                if oc_r.status_code == 200:
                    oc_data = oc_r.json()
                    # If opencode returned something real, pass it through
                    items = oc_data if isinstance(oc_data, list) else oc_data.get("files", oc_data.get("entries", []))
                    if items:
                        return JSONResponse(content=oc_data, status_code=200)
            except Exception:
                pass
            # Fallback: return workspace entries
            entries = list_workspace_folders(WORKSPACE_PATH)
            if query:
                entries = [e for e in entries if query in e["name"].lower()]
            return JSONResponse(content=entries)

        # ── Intercept /path — fix garbled directory, always pass through ──
        if path == "path":
            directory = request.query_params.get("directory", "")
            # Fix garbled unicode or empty directory
            if not directory or "\ufffd" in directory or len(directory.strip()) < 2:
                directory = WORKSPACE_PATH
            fwd_headers = dict(request.headers)
            fwd_headers.pop("host", None)
            fwd_headers["authorization"] = _OC_AUTH_HEADER
            try:
                import urllib.parse
                oc_url = f"/path?directory={urllib.parse.quote(directory, safe='/')}"
                oc_req = http_client.build_request("GET", oc_url, headers=fwd_headers)
                oc_r = await http_client.send(oc_req)
                if oc_r.status_code == 200:
                    # Always pass through opencode's real response
                    from fastapi.responses import Response as RawResponse
                    return RawResponse(
                        content=oc_r.content,
                        status_code=200,
                        media_type="application/json",
                    )
            except Exception:
                pass
            # Fallback with correct opencode format {path, home, root, entries}
            return JSONResponse(content=make_path_response(directory))

        # ── All other requests: transparent proxy ─────────────────────
        headers = dict(request.headers)
        headers.pop("host", None)
        headers["authorization"] = _OC_AUTH_HEADER
        content = await request.body()

        url = f"/{path}"
        if request.url.query:
            url += f"?{request.url.query}"

        req = http_client.build_request(
            method=request.method,
            url=url,
            headers=headers,
            content=content,
        )

        try:
            r = await http_client.send(req, stream=True)

            async def stream_bytes():
                try:
                    async for chunk in r.aiter_bytes():
                        yield chunk
                finally:
                    await r.aclose()

            response_headers = dict(r.headers)
            response_headers.pop("content-length", None)

            return StreamingResponse(
                stream_bytes(),
                status_code=r.status_code,
                headers=response_headers,
                media_type=r.headers.get("content-type"),
            )
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Proxy error: {str(e)}")

except Exception:
    app = None
