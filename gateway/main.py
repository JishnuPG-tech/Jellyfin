"""
OpenCode Space — Production Gateway Main Application
=====================================================
Assembles Service Routers into a unified FastAPI ASGI Gateway:
  1. Lightweight Public Readiness: /health/live (HTTP 200 {"status": "alive"})
  2. Root Portal Hub: / -> Multi-Service Dashboard
  3. OmniRoute Gateway (gateway.omniroute) -> /v1, /v1beta, /dashboard, /api/providers, /api/oauth, /live-ws
  4. Hermes Autonomous Agent (gateway.hermes) -> /hermes/v1/* (8642)
  5. Jellyfin Media Server (gateway.jellyfin) -> /jellyfin (8096)
  6. TG-Drive Direct Streamer (gateway.tg_stream) -> /tg_stream (8080)
"""

import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse, RedirectResponse

from gateway.utils import (
    get_http_client,
    proxy_http_request,
    get_structured_logger,
    HERMES_PORT,
    JELLYFIN_PORT,
    TG_PORT,
    OMNIROUTE_PORT,
    PUBLIC_HOST,
)
from gateway.omniroute import router as omniroute_router, omniroute_main_route
from gateway.jellyfin import router as jellyfin_router
from gateway.tg_stream import router as tg_stream_router
from gateway.hermes import router as hermes_router

logger = get_structured_logger("GatewayMain")

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing Gateway Connection Pool...")
    client = get_http_client()
    yield
    logger.info("Closing Gateway Connection Pool...")
    if not client.is_closed:
        await client.aclose()


app = FastAPI(title="OpenCode Space Gateway", lifespan=lifespan, docs_url=None, redoc_url=None)

# Include Service Routers
app.include_router(omniroute_router)
app.include_router(jellyfin_router)
app.include_router(tg_stream_router)
app.include_router(hermes_router)


# ── Lightweight Platform Readiness Endpoint ──────────────────────────────────
@app.get("/health/live")
@app.head("/health/live")
async def health_live():
    """
    Lightweight platform readiness check required for Hugging Face Spaces.
    Must return immediately with HTTP 200 without blocking on downstream initialization.
    """
    return JSONResponse(content={"status": "alive"}, status_code=200)


# ── Root Landing & Diagnostic Routes ─────────────────────────────────────────
@app.api_route("/favicon.ico", methods=["GET", "HEAD"])
@app.api_route("/favicon.png", methods=["GET", "HEAD"])
@app.api_route("/favicon.svg", methods=["GET", "HEAD"])
@app.api_route("/static/favicon.png", methods=["GET", "HEAD"])
@app.api_route("/static/favicon.ico", methods=["GET", "HEAD"])
@app.api_route("/omniroute/static/favicon.png", methods=["GET", "HEAD"])
@app.api_route("/omniroute/static/favicon.ico", methods=["GET", "HEAD"])
async def favicon():
    return Response(content=b"", status_code=204)

@app.api_route("/icon-512.png", methods=["GET", "HEAD"])
@app.api_route("/icon-192.png", methods=["GET", "HEAD"])
@app.api_route("/omniroute/icon-512.png", methods=["GET", "HEAD"])
@app.api_route("/omniroute/icon-192.png", methods=["GET", "HEAD"])
async def icons():
    return Response(content=b"", status_code=204)

@app.api_route("/offline", methods=["GET", "HEAD"])
@app.api_route("/omniroute/offline", methods=["GET", "HEAD"])
async def offline_page():
    return HTMLResponse(content="<!DOCTYPE html><html><head><title>OpenCode Space</title></head><body style='background:#0f172a;color:#f8fafc;font-family:system-ui;text-align:center;padding:50px;'><h2>OpenCode Space Gateway Online</h2><p><a href='/dashboard' style='color:#38bdf8;'>Go to OmniRoute Dashboard</a></p></body></html>", status_code=200)

@app.api_route("/manifest.json", methods=["GET", "HEAD"])
@app.api_route("/manifest.webmanifest", methods=["GET", "HEAD"])
@app.api_route("/site.webmanifest", methods=["GET", "HEAD"])
@app.api_route("/omniroute/manifest.json", methods=["GET", "HEAD"])
@app.api_route("/omniroute/manifest.webmanifest", methods=["GET", "HEAD"])
@app.api_route("/omniroute/site.webmanifest", methods=["GET", "HEAD"])
async def webmanifest():
    return JSONResponse(
        content={
            "name": "OpenCode Space",
            "short_name": "OpenCode",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#0f172a",
            "theme_color": "#0f172a",
            "icons": [
                {
                    "src": "/static/favicon.png",
                    "sizes": "192x192",
                    "type": "image/png"
                }
            ]
        },
        status_code=200
    )

@app.get("/health")
@app.get("/debug/status")
async def health_check():
    client = get_http_client()
    services = {
        "omniroute": f"http://127.0.0.1:{OMNIROUTE_PORT}/",
        "hermes":    f"http://127.0.0.1:{HERMES_PORT}/health",
        "jellyfin":  f"http://127.0.0.1:{JELLYFIN_PORT}/",
        "tg_stream": f"http://127.0.0.1:{TG_PORT}/",
    }
    results = {}
    for name, url in services.items():
        try:
            r = await client.get(url, timeout=2.0)
            results[name] = {"status": "ok", "code": r.status_code}
        except Exception as exc:
            results[name] = {"status": "starting", "message": str(exc)}
    return {"gateway": "healthy", "upstreams": results}


# ── Root Portal Route ────────────────────────────────────────────────────────
@app.get("/")
@app.get("/index.html")
async def root_portal():
    for candidate in ("/index.html", "index.html", os.path.join(os.path.dirname(__file__), "..", "index.html")):
        if os.path.exists(candidate):
            return FileResponse(candidate, media_type="text/html")
    return HTMLResponse(content="<h1>OpenCode Space Gateway Online</h1><p><a href='/dashboard'>OmniRoute Dashboard</a></p>", status_code=200)


# ── Catch-All Referer & Subpath Fallback Router ──────────────────────────────
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def route_catch_all(path: str, request: Request):
    referer = request.headers.get("referer", "").lower()
    req_path = request.url.path.lower()

    if req_path in ("/", "/index.html"):
        return await root_portal()

    is_omniroute_referer = any(p in referer for p in ("/dashboard", "/omniroute", "/providers", "/home", "/connections", "/settings", "/login", "/setup", "/wizard", "/keys", "/combos", "/logs", "/stats", "/arena", "/pricing"))

    OMNIROUTE_PREFIXES = (
        "/dashboard",
        "/omniroute",
        "/home",
        "/login",
        "/setup",
        "/wizard",
        "/callback",
        "/_next",
        "/providers",
        "/connections",
        "/custom-models",
        "/synced-models",
        "/models",
        "/settings",
        "/combos",
        "/keys",
        "/stats",
        "/logs",
        "/arena",
        "/pricing",
        "/api/providers",
        "/api/provider-",
        "/api/models",
        "/api/model",
        "/api/credentials",
        "/api/connections",
        "/api/custom-",
        "/api/synced-",
        "/api/token-",
        "/api/sync/",
        "/api/oauth",
        "/api/settings",
        "/api/monitoring",
        "/api/combos",
        "/api/keys",
        "/api/stats",
        "/api/health",
        "/api/system",
        "/api/logs",
        "/api/vector",
        "/api/tokens",
        "/api/cloud-",
        "/api/arena",
        "/api/pricing",
    )

    OMNIROUTE_EXACT = (
        "/dashboard",
        "/home",
        "/login",
        "/setup",
        "/wizard",
        "/providers",
        "/connections",
        "/custom-models",
        "/synced-models",
        "/models",
        "/settings",
        "/combos",
        "/keys",
        "/stats",
        "/logs",
        "/arena",
        "/pricing",
    )

    extra = {
        "Host": PUBLIC_HOST,
        "X-Forwarded-Host": PUBLIC_HOST,
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Port": "443",
    }

    # If request originates from OmniRoute Dashboard or matches OmniRoute prefixes, route to OmniRoute
    if (
        is_omniroute_referer
        or any(req_path == p or req_path.startswith(p) for p in OMNIROUTE_PREFIXES)
        or req_path in OMNIROUTE_EXACT
    ):
        if not (req_path.startswith("/jellyfin") or req_path.startswith("/tg-stream") or req_path.startswith("/tg_stream") or req_path.startswith("/hermes") or req_path == "/health/live"):
            logger.info(f"[ROUTER] {req_path} (referer={referer}) -> OmniRoute ({OMNIROUTE_PORT})")
            res = await proxy_http_request(f"http://127.0.0.1:{OMNIROUTE_PORT}{req_path}", request, default_prefix="", extra_headers=extra)
            if res.status_code in (401, 403) and not req_path.startswith("/api/v1/auths"):
                return JSONResponse(content={"status": "ok", "authenticated": False, "message": "unauthenticated"}, status_code=200)
            return res

    # ── 2. Jellyfin Media Server Namespace ────────────────────────────────────
    if req_path == "/jellyfin" or req_path.startswith("/jellyfin/"):
        logger.info(f"[ROUTER] {req_path} -> Jellyfin ({JELLYFIN_PORT})")
        sub_p = "/" if req_path == "/jellyfin" else req_path[len("/jellyfin"):]
        return await proxy_http_request(f"http://127.0.0.1:{JELLYFIN_PORT}{sub_p}", request, default_prefix="/jellyfin", extra_headers={"X-Forwarded-Prefix": "/jellyfin"})

    # ── 3. Telegram Streamer Namespace ────────────────────────────────────────
    if req_path in ("/tg-stream", "/tg_stream") or req_path.startswith("/tg-stream/") or req_path.startswith("/tg_stream/"):
        logger.info(f"[ROUTER] {req_path} -> Telegram ({TG_PORT})")
        if req_path in ("/tg-stream", "/tg_stream"):
            sub_p = "/"
        elif req_path.startswith("/tg-stream/"):
            sub_p = req_path[len("/tg-stream"):]
        else:
            sub_p = req_path[len("/tg_stream"):]
        return await proxy_http_request(f"http://127.0.0.1:{TG_PORT}{sub_p}", request, default_prefix="/tg-stream")

    # ── 4. Default Fallback ───────────────────────────────────────────────────
    if request.method == "GET" and "html" in request.headers.get("accept", "").lower():
        return await root_portal()
    
    return JSONResponse(content={"status": "error", "message": f"Route not found: {req_path}"}, status_code=404)
