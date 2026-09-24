import os
import logging
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from gateway.utils import get_http_client, proxy_http_request
from gateway.ops import router as ops_router

logger = logging.getLogger("gateway.main")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="OpenCode Space Gateway", docs_url=None, redoc_url=None)

app.include_router(ops_router)

JELLYFIN_PORT = 8096
TG_PORT = 8080

@app.get("/health")
@app.get("/debug/status")
async def health_check():
    client = get_http_client()
    services = {
        "jellyfin":  f"http://127.0.0.1:{JELLYFIN_PORT}/System/Info/Public",
        "tg_stream": f"http://127.0.0.1:{TG_PORT}/health",
    }
    results = {}
    for name, url in services.items():
        try:
            r = await client.get(url, timeout=2.0)
            results[name] = {"status": "ok", "code": r.status_code}
        except Exception as exc:
            results[name] = {"status": "starting", "message": str(exc)}
    return {"gateway": "healthy", "upstreams": results}

@app.get("/")
@app.get("/index.html")
async def root_portal():
    html_content = '''<!DOCTYPE html><html><head><title>Jellyfin Streamer</title></head><body style='background:#0f172a;color:#f8fafc;font-family:system-ui;text-align:center;padding:50px;'><h2>Gateway Online</h2><p><a href='/jellyfin/' style='color:#38bdf8;'>Go to Jellyfin</a></p></body></html>'''
    return HTMLResponse(content=html_content, status_code=200)

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def route_catch_all(path: str, request: Request):
    req_path = request.url.path.lower()
    
    if req_path in ("/", "/index.html"):
        return await root_portal()
    
    if req_path == "/jellyfin" or req_path.startswith("/jellyfin/"):
        logger.info(f"[ROUTER] {req_path} -> Jellyfin ({JELLYFIN_PORT})")
        sub_p = "/" if req_path == "/jellyfin" else req_path[len("/jellyfin"):]
        return await proxy_http_request(f"http://127.0.0.1:{JELLYFIN_PORT}{sub_p}", request, default_prefix="/jellyfin", extra_headers={"X-Forwarded-Prefix": "/jellyfin"})
    
    if req_path in ("/tg-stream", "/tg_stream") or req_path.startswith("/tg-stream/") or req_path.startswith("/tg_stream/"):
        logger.info(f"[ROUTER] {req_path} -> Telegram ({TG_PORT})")
        if req_path in ("/tg-stream", "/tg_stream"):
            sub_p = "/"
        elif req_path.startswith("/tg-stream/"):
            sub_p = req_path[len("/tg-stream"):]
        else:
            sub_p = req_path[len("/tg_stream"):]
        return await proxy_http_request(f"http://127.0.0.1:{TG_PORT}{sub_p}", request, default_prefix="/tg-stream")
    
    if req_path == "/health/live":
        return JSONResponse({"status": "live"})
        
    return JSONResponse(content={"status": "error", "message": f"Route not found: {req_path}"}, status_code=404)
