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

    # ── Terminal (ttyd) reverse proxy ──────────────────────────────────
    TTYD_PORT = int(os.environ.get("TTYD_PORT", "7681"))

    from fastapi.responses import HTMLResponse

    TERMINAL_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>OpenCode Terminal</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        html, body { height: 100%; background: #000; color: #c8c8c8; font-family: 'JetBrains Mono', monospace; font-size: 13px; line-height: 1.45; overflow: hidden; }
        #topbar { height: 32px; background: #0a0a0a; border-bottom: 1px solid #1a1a1a; display: flex; align-items: center; padding: 0 12px; gap: 10px; flex-shrink: 0; }
        #topbar-title { color: #555; font-size: 11px; letter-spacing: 0.5px; text-transform: uppercase; }
        #topbar-status { margin-left: auto; display: flex; align-items: center; gap: 6px; font-size: 11px; color: #444; }
        #ws-dot { width: 6px; height: 6px; border-radius: 50%; background: #333; }
        #ws-dot.connected { background: #22c55e; box-shadow: 0 0 6px #22c55e; }
        #ws-dot.connecting { background: #eab308; animation: pulse 1s infinite; }
        @keyframes pulse { 50% { opacity: 0.4; } }
        #terminal { position: absolute; top: 32px; left: 0; right: 0; bottom: 0; }
    </style>
</head>
<body>
<div id="topbar">
    <span id="topbar-title">opencode terminal</span>
    <div id="topbar-status">
        <div id="ws-dot"></div>
        <span id="ws-label">connecting</span>
    </div>
</div>
<div id="terminal"></div>
<script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js"></script>
<script>
(function() {
    const term = new Terminal({ cursorBlink: true, fontSize: 13, fontFamily: 'JetBrains Mono', theme: { background: '#000', foreground: '#c8c8c8', cursor: '#c8c8c8' } });
    const fitAddon = new FitAddon.FitAddon();
    term.loadAddon(fitAddon);
    term.open(document.getElementById('terminal'));
    fitAddon.fit();
    window.addEventListener('resize', () => fitAddon.fit());

    const wsDot = document.getElementById('ws-dot');
    const wsLabel = document.getElementById('ws-label');
    let ws = null, reconnectTimer = null;

    function connect() {
        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        ws = new WebSocket(`${proto}//${location.host}/terminal/ws`);
        ws.binaryType = 'arraybuffer';
        wsDot.className = 'connecting';
        wsLabel.textContent = 'connecting';

        ws.onopen = () => { wsDot.className = 'connected'; wsLabel.textContent = 'connected'; };
        ws.onmessage = (e) => { term.write(new Uint8Array(e.data)); };
        ws.onclose = () => { wsDot.className = ''; wsLabel.textContent = 'disconnected'; if (reconnectTimer) clearTimeout(reconnectTimer); reconnectTimer = setTimeout(connect, 3000); };
        ws.onerror = () => ws.close();
    }
    connect();
    term.onData((data) => { if (ws && ws.readyState === 1) ws.send(data); });
})();
</script>
</body>
</html>"""

    @app.get("/terminal", response_class=HTMLResponse)
    async def terminal_page():
        """Serve the xterm.js terminal page."""
        return TERMINAL_HTML

    @app.websocket("/terminal/ws")
    async def terminal_ws_proxy(client_ws: WebSocket):
        """Proxy WebSocket to ttyd."""
        await client_ws.accept()
        uri = f"ws://127.0.0.1:{TTYD_PORT}/terminal/ws"
        if client_ws.query_params:
            uri += f"?{client_ws.query_params}"
        try:
            async with websockets.connect(uri) as server_ws:
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
        import logging
        wm = get_workspace_manager()
        try:
            info = await wm.ensure_running(workspace_id)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except Exception as e:
            logging.error(f"proxy ensure_running error: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to start workspace: {str(e)}")

        port = info["port"]

        headers = dict(request.headers)
        headers.pop("host", None)
        headers["authorization"] = _OC_AUTH_HEADER
        content = await request.body()

        url = f"/{path}"
        if request.url.query:
            url += f"?{request.url.query}"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
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
        except httpx.ConnectError as e:
            logging.error(f"proxy connect error to port {port}: {e}")
            raise HTTPException(status_code=502, detail=f"Workspace not reachable on port {port}: {str(e)}")
        except Exception as e:
            logging.error(f"proxy error: {e}")
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
