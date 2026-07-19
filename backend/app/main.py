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
    def list_workspace_entries(base_path: str = None, query: str = ""):
        """Return workspace folder entries matching opencode's find/file format: ['name/', ...]."""
        base_path = base_path or WORKSPACE_PATH
        entries = []
        try:
            for entry in sorted(os.scandir(base_path), key=lambda e: e.name):
                if entry.name.startswith("."):
                    continue
                if entry.is_dir(follow_symlinks=True):
                    name = entry.name + "/"
                else:
                    name = entry.name
                if not query or query.lower() in name.lower():
                    entries.append(name)
        except Exception:
            pass
        return entries

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

        # ── Intercept /find/file — opencode returns ["folder/", ...] string array
        if path == "find/file":
            query = request.query_params.get("query", "")
            directory = request.query_params.get("directory", "")
            # Fix garbled/missing directory
            if not directory or "\ufffd" in directory:
                directory = WORKSPACE_PATH
            # Forward to opencode first
            try:
                import urllib.parse
                oc_url = f"/find/file?directory={urllib.parse.quote(directory, safe='/')}"
                if query:
                    oc_url += f"&query={urllib.parse.quote(query)}"
                # Preserve other params (type, limit)
                for k, v in request.query_params.items():
                    if k not in ("directory", "query"):
                        oc_url += f"&{k}={urllib.parse.quote(v)}"
                fwd_headers = dict(request.headers)
                fwd_headers.pop("host", None)
                fwd_headers["authorization"] = _OC_AUTH_HEADER
                oc_req = http_client.build_request("GET", oc_url, headers=fwd_headers)
                oc_r = await http_client.send(oc_req)
                if oc_r.status_code == 200:
                    oc_data = oc_r.json()
                    # opencode returns a plain list of strings like ['my-code/', 'projects/']
                    if isinstance(oc_data, list) and oc_data:
                        return JSONResponse(content=oc_data)
            except Exception:
                pass
            # Fallback: return workspace entries in same string-array format
            entries = list_workspace_entries(directory, query)
            return JSONResponse(content=entries)

        # NOTE: /path is just metadata ({home, config, worktree, directory})
        # NOT a folder listing. Don't intercept it — pass directly through.


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
