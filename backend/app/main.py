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
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse
    from backend.app.api import router as api_router
    from backend.app.webapp_html import HTML_CONTENT

    app = FastAPI(title="Opencode Bridge", lifespan=lifespan)
    app.include_router(api_router, prefix="/api")

    # ── Transparent Reverse Proxy to local opencode serve (port 4096) ──
    import httpx
    import asyncio
    import websockets
    from fastapi import Request, HTTPException, WebSocket
    from fastapi.responses import StreamingResponse

    @app.websocket("/{path:path}")
    async def websocket_proxy(client_ws: WebSocket, path: str):
        await client_ws.accept()
        uri = f"ws://127.0.0.1:4096/{path}"
        if client_ws.query_params:
            uri += f"?{client_ws.query_params}"
            
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

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
    async def proxy_request(request: Request, path: str):
        if request.headers.get("upgrade", "").lower() == "websocket":
            raise HTTPException(status_code=400, detail="Websocket upgrade not supported on proxy")
            
        headers = dict(request.headers)
        headers.pop("host", None)
        content = await request.body()
        
        async def stream_response():
            async with httpx.AsyncClient() as c:
                req = c.build_request(
                    method=request.method,
                    url=f"http://127.0.0.1:4096/{path}" + (f"?{request.url.query}" if request.url.query else ""),
                    headers=headers,
                    content=content,
                    timeout=120.0
                )
                r = await c.send(req, stream=True)
                yield r
                
        try:
            stream_gen = stream_response()
            r = await stream_gen.__anext__()
            
            response_headers = dict(r.headers)
            response_headers.pop("content-length", None)
            
            return StreamingResponse(
                r.aiter_bytes(),
                status_code=r.status_code,
                headers=response_headers,
                media_type=r.headers.get("content-type")
            )
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Proxy error: {str(e)}")
except Exception:
    app = None
