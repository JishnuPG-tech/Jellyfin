"""
Jellyfin Media Server Gateway Router
=====================================
Routes /jellyfin/ and /Jellyfin/ to Jellyfin Media Server (port 8096).
Handles video range headers & X-Forwarded-Prefix.
"""

from fastapi import APIRouter, Request
from gateway.utils import JELLYFIN_PORT, proxy_http_request

router = APIRouter(tags=["Jellyfin"])

@router.api_route("/jellyfin", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
@router.api_route("/jellyfin/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
@router.api_route("/Jellyfin", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
@router.api_route("/Jellyfin/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def jellyfin_main_route(request: Request, path: str = ""):
    target = f"http://127.0.0.1:{JELLYFIN_PORT}/{path}"
    return await proxy_http_request(
    target,
    request,
    default_prefix="/jellyfin",
    extra_headers={
        "X-Forwarded-Prefix": "/jellyfin",
        "Accept-Encoding": "identity",
    },
)
