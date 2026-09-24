"""
Telegram Direct Range Streamer Gateway Router
=============================================
Routes /tg-stream/ to TG-Drive High-Speed 5G Streamer (port 8080).
Supports high-speed video chunk streaming and range headers.
"""

from fastapi import APIRouter, Request
from gateway.utils import TG_PORT, proxy_http_request

router = APIRouter(tags=["TGStreamer"])

@router.api_route("/tg-stream", methods=["GET", "HEAD", "OPTIONS"])
@router.api_route("/tg-stream/{path:path}", methods=["GET", "HEAD", "OPTIONS"])
async def tg_stream_main_route(request: Request, path: str = ""):
    target = f"http://127.0.0.1:{TG_PORT}/{path}"
    return await proxy_http_request(target, request, default_prefix="/tg-stream")
