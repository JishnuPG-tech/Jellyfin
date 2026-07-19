import os
from contextlib import asynccontextmanager
from backend.app.handlers import healthz
from bot.telegram_bot import run_bot_async, stop_bot_async


@asynccontextmanager
async def lifespan(app):
    import asyncio
    # Start Telegram Bot in the background using FastAPI's event loop
    bot_task = asyncio.create_task(run_bot_async())
    yield
    # Stop Telegram Bot gracefully
    await stop_bot_async()
    bot_task.cancel()
    try:
        await bot_task
    except asyncio.CancelledError:
        pass


try:
    import base64
    import httpx
    import asyncio
    import websockets
    from fastapi import FastAPI, Request, HTTPException, WebSocket
    from fastapi.responses import StreamingResponse, HTMLResponse
    from backend.app.api import router as api_router
    from backend.app.webapp_html import HTML_CONTENT

    # ── Auth credentials for opencode serve ──────────────────────────
    _OC_USERNAME = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
    _OC_PASSWORD = os.environ.get("OPENCODE_SERVER_PASSWORD", "password")
    _OC_AUTH_B64 = base64.b64encode(f"{_OC_USERNAME}:{_OC_PASSWORD}".encode()).decode()
    _OC_AUTH_HEADER = f"Basic {_OC_AUTH_B64}"

    # ── Global persistent HTTP client ────────────────────────────────
    http_client = httpx.AsyncClient(
        base_url="http://127.0.0.1:4096",
        timeout=120.0,
        headers={"Authorization": _OC_AUTH_HEADER},
    )

    app = FastAPI(title="Opencode Bridge", lifespan=lifespan)
    app.include_router(api_router, prefix="/api")

    # ── WebSocket reverse proxy ──────────────────────────────────────
    @app.websocket("/{path:path}")
    async def websocket_proxy(client_ws: WebSocket, path: str):
        await client_ws.accept()
        uri = f"ws://127.0.0.1:4096/{path}"
        if client_ws.query_params:
            uri += f"?{client_ws.query_params}"

        extra_headers = {"Authorization": _OC_AUTH_HEADER}
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

    # ── HTTP reverse proxy ───────────────────────────────────────────
    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
    async def proxy_request(request: Request, path: str):
        if request.headers.get("upgrade", "").lower() == "websocket":
            raise HTTPException(status_code=400, detail="WebSocket upgrade must use ws:// endpoint")

        headers = dict(request.headers)
        headers.pop("host", None)
        # Always inject our server auth – overrides anything the browser sends
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

            # Ensure connection is always released after streaming
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
