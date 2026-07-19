import os
import json
import base64
from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app):
    import asyncio
    import logging

    # Start workspace manager
    wm = None
    try:
        from services.workspace_process_manager import get_workspace_manager
        wm = get_workspace_manager()
        wm.start_idle_checker()
    except Exception as e:
        logging.error(f"Failed to init workspace manager: {e}")

    # Start Telegram bot
    bot_task = None
    try:
        from bot.telegram_bot import run_bot_async
        bot_task = asyncio.create_task(run_bot_async())
    except Exception as e:
        logging.error(f"Failed to start Telegram bot: {e}")

    yield

    # Shutdown
    try:
        from bot.telegram_bot import stop_bot_async
        await stop_bot_async()
    except Exception:
        pass
    if bot_task:
        bot_task.cancel()
        try:
            await bot_task
        except asyncio.CancelledError:
            pass
    if wm:
        try:
            await wm.shutdown_all()
        except Exception:
            pass


try:
    import httpx
    import asyncio
    import websockets
    from fastapi import FastAPI, Request, HTTPException, WebSocket
    from fastapi.responses import StreamingResponse, JSONResponse
    from backend.app.api import router as api_router
    from bot.telegram_bot import run_bot_async, stop_bot_async
    from services.workspace_process_manager import get_workspace_manager

    # ── Auth credentials for opencode serve ──────────────────────────
    _OC_USERNAME = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
    _OC_PASSWORD = os.environ.get("OPENCODE_SERVER_PASSWORD", "password")
    _OC_AUTH_B64 = base64.b64encode(f"{_OC_USERNAME}:{_OC_PASSWORD}".encode()).decode()
    _OC_AUTH_HEADER = f"Basic {_OC_AUTH_B64}"

    WORKSPACE_PATH = os.environ.get("WORKSPACE_PATH", "/data/workspaces")

    app = FastAPI(title="Opencode Bridge", lifespan=lifespan)
    app.include_router(api_router, prefix="/api")

    # ── Helper: get opencode port for a workspace ────────────────────
    def _get_opencode_port(workspace_id: str = None) -> int:
        """Get the port for a specific workspace, or default for backward compat."""
        if workspace_id:
            wm = get_workspace_manager()
            ws = wm.registry.get(workspace_id)
            if ws and ws.get("port"):
                return ws["port"]
        return 4096  # fallback

    def _get_auth_for_port(port: int) -> str:
        return _OC_AUTH_HEADER

    # ── Folder helpers ───────────────────────────────────────────────
    def list_workspace_entries(base_path: str = None, query: str = ""):
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

    # ── WebSocket reverse proxy (per-workspace) ──────────────────────
    @app.websocket("/ws/{workspace_id}/{path:path}")
    async def websocket_proxy_workspace(client_ws: WebSocket, workspace_id: str, path: str):
        await client_ws.accept()
        wm = get_workspace_manager()
        try:
            info = await wm.ensure_running(workspace_id)
        except ValueError as e:
            await client_ws.close(code=1008, reason=str(e))
            return

        port = info["port"]
        uri = f"ws://127.0.0.1:{port}/{path}"
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

    # ── Legacy WebSocket proxy (single workspace, backward compat) ───
    @app.websocket("/{path:path}")
    async def websocket_proxy(client_ws: WebSocket, path: str):
        if path.startswith("ws/"):
            # Already handled by workspace-specific route
            return
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
    @app.api_route("/proxy/{workspace_id}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
    async def proxy_to_workspace(request: Request, workspace_id: str, path: str):
        """Proxy requests to a specific workspace's opencode instance."""
        wm = get_workspace_manager()
        try:
            info = await wm.ensure_running(workspace_id)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

        port = info["port"]

        headers = dict(request.headers)
        headers.pop("host", None)
        headers["authorization"] = _OC_AUTH_HEADER
        content = await request.body()

        url = f"/{path}"
        if request.url.query:
            url += f"?{request.url.query}"

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                r = await client.request(
                    method=request.method,
                    url=f"http://127.0.0.1:{port}{url}",
                    headers=headers,
                    content=content,
                )

                async def stream_bytes():
                    for chunk in r.iter_bytes():
                        yield chunk

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

    # ── Default HTTP reverse proxy (backward compat, single workspace) ─
    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
    async def proxy_request(request: Request, path: str):
        if request.headers.get("upgrade", "").lower() == "websocket":
            raise HTTPException(status_code=400, detail="WebSocket upgrade must use ws:// endpoint")

        # Skip api routes and proxy routes
        if path.startswith("api/") or path.startswith("proxy/"):
            raise HTTPException(status_code=404, detail="Not found")

        # ── Intercept /find/file — opencode returns ["folder/", ...] string array
        if path == "find/file":
            query = request.query_params.get("query", "")
            directory = request.query_params.get("directory", "")
            if not directory or "\ufffd" in directory:
                directory = WORKSPACE_PATH
            try:
                import urllib.parse
                oc_url = f"/find/file?directory={urllib.parse.quote(directory, safe='/')}"
                if query:
                    oc_url += f"&query={urllib.parse.quote(query)}"
                for k, v in request.query_params.items():
                    if k not in ("directory", "query"):
                        oc_url += f"&{k}={urllib.parse.quote(v)}"
                fwd_headers = dict(request.headers)
                fwd_headers.pop("host", None)
                fwd_headers["authorization"] = _OC_AUTH_HEADER
                async with httpx.AsyncClient(base_url="http://127.0.0.1:4096", timeout=30.0) as client:
                    oc_r = await client.get(oc_url, headers=fwd_headers)
                    if oc_r.status_code == 200:
                        oc_data = oc_r.json()
                        if isinstance(oc_data, list) and oc_data:
                            return JSONResponse(content=oc_data)
            except Exception:
                pass
            entries = list_workspace_entries(directory, query)
            return JSONResponse(content=entries)

        # ── All other requests: transparent proxy to default (port 4096)
        headers = dict(request.headers)
        headers.pop("host", None)
        headers["authorization"] = _OC_AUTH_HEADER
        content = await request.body()

        url = f"/{path}"
        if request.url.query:
            url += f"?{request.url.query}"

        try:
            async with httpx.AsyncClient(base_url="http://127.0.0.1:4096", timeout=120.0) as client:
                r = await client.request(
                    method=request.method,
                    url=url,
                    headers=headers,
                    content=content,
                )

                async def stream_bytes():
                    for chunk in r.iter_bytes():
                        yield chunk

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
