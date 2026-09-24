"""
Gateway Proxy Core Utilities
============================
High-Performance Async HTTP client, Header Sanitization, Location Header Rewriting,
and Core HTTP & WebSocket proxying logic.
"""

import os
import re
import asyncio
import logging
from typing import Callable, Optional, Dict, Any

import httpx
import aiohttp
from fastapi import Request, Response, WebSocket
from fastapi.responses import StreamingResponse

def get_structured_logger(name: str) -> logging.Logger:
    log = logging.getLogger(name)
    if not log.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter("%(asctime)s [%(levelname)s] [%(name)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        handler.setFormatter(formatter)
        log.addHandler(handler)
        log.setLevel(logging.INFO)
    return log

logger = get_structured_logger("GatewayUtils")

# Port definitions
OMNIROUTE_PORT     = int(os.environ.get("OMNIROUTE_PORT", 20128))
OMNIROUTE_API_PORT = int(os.environ.get("OMNIROUTE_API_PORT", 20129))
OMNIROUTE_WS_PORT  = int(os.environ.get("OMNIROUTE_WS_PORT", 20132))
HERMES_PORT        = int(os.environ.get("HERMES_PORT", 8642))
JELLYFIN_PORT      = int(os.environ.get("JELLYFIN_PORT", 8096))
TG_PORT            = int(os.environ.get("TG_PORT", 8080))
GATEWAY_PORT       = int(os.environ.get("GATEWAY_PORT", 8000))

PUBLIC_HOST   = os.environ.get("PUBLIC_HOST", "jishnupg-opencode-cli.hf.space")
PUBLIC_ORIGIN = f"https://{PUBLIC_HOST}"

_INTERNAL_PORTS = {GATEWAY_PORT, OMNIROUTE_PORT, OMNIROUTE_API_PORT, OMNIROUTE_WS_PORT, HERMES_PORT, JELLYFIN_PORT, TG_PORT}
_PORT_STRIP_RE  = re.compile(r"(https?://[^/:]+):(" + "|".join(str(p) for p in _INTERNAL_PORTS) + r")")

_PORT_PREFIX_MAP = {
    GATEWAY_PORT:       "",
    OMNIROUTE_PORT:     "",
    OMNIROUTE_API_PORT: "",
    OMNIROUTE_WS_PORT:  "/live-ws",
    HERMES_PORT:        "/hermes",
    JELLYFIN_PORT:      "/jellyfin",
    TG_PORT:            "/tg-stream",
}

# Shared Async HTTPX Client Instance
http_client: Optional[httpx.AsyncClient] = None

def get_http_client() -> httpx.AsyncClient:
    global http_client
    if http_client is None or http_client.is_closed:
        http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=300.0, write=300.0, pool=30.0),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=1000, max_keepalive_connections=200, keepalive_expiry=30.0),
        )
    return http_client

def fix_location_header(location: str, default_prefix: str = "") -> str:
    """Rewrite Location header so internal ports or root paths stay prefixed correctly."""
    if not location:
        return location

    # If full URL containing internal port:
    def _replace_port(match: re.Match[str]) -> str:
        port = int(match.group(2))
        prefix = _PORT_PREFIX_MAP.get(port, default_prefix)
        return f"{PUBLIC_ORIGIN}{prefix}"

    rewritten = _PORT_STRIP_RE.sub(_replace_port, location)

    # Handle relative redirects (e.g., Location: /dashboard or Location: /auth)
    if default_prefix and rewritten.startswith("/") and not rewritten.startswith(default_prefix) and not rewritten.startswith("http"):
        rewritten = f"{default_prefix}{rewritten}"

    return rewritten


_HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
    "content-length", "content-encoding"
}

def get_client_ip(request: Request) -> str:
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip:
        return cf_ip.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        ips = [ip.strip() for ip in xff.split(",") if ip.strip() and ip.strip() != "127.0.0.1"]
        if ips:
            return ips[0]
    x_real = request.headers.get("x-real-ip")
    if x_real and x_real != "127.0.0.1":
        return x_real.strip()
    if request.client and request.client.host and request.client.host != "127.0.0.1":
        return request.client.host
    return "127.0.0.1"

def build_upstream_headers(request: Request, extra_headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    headers = {
    k: v for k, v in request.headers.items()
    if k.lower() not in _HOP_BY_HOP_HEADERS
    and k.lower() not in ("host", "user-agent", "accept-encoding")
} 
    client_ip = get_client_ip(request)
    headers["Host"]              = PUBLIC_HOST
    headers["X-Forwarded-Host"]  = PUBLIC_HOST
    headers["X-Forwarded-Proto"] = "https"
    headers["X-Forwarded-Port"]  = "443"
    headers["X-Real-IP"]         = client_ip
    headers["X-Forwarded-For"]   = f"{client_ip}, 127.0.0.1"
    headers["User-Agent"]        = request.headers.get("user-agent") or "Python-urllib/3.11"
    # Prevent compressed upstream responses from being forwarded incorrectly.
    # The proxy strips Content-Encoding from downstream responses.
    headers["Accept-Encoding"] = "identity"

    if extra_headers:
        for ek, ev in extra_headers.items():
            for existing_k in list(headers.keys()):
                if existing_k.lower() == ek.lower():
                    del headers[existing_k]
            headers[ek] = ev
    return headers

def build_downstream_raw_headers(resp_headers: httpx.Headers, default_prefix: str = "") -> list:
    raw_headers = []
    has_csp = False
    has_cors = False
    for key, value in resp_headers.multi_items():
        lk = key.lower()
        if lk in _HOP_BY_HOP_HEADERS:
            continue
        if lk == "x-frame-options":
            # Strip restrictive frame options to allow Hugging Face Space iframe embedding
            continue
        if lk == "access-control-allow-origin":
            has_cors = True
        if lk == "content-security-policy":
            has_csp = True
            # Update frame-ancestors directive to permit https://huggingface.co
            if "frame-ancestors" in value.lower():
                value = re.sub(
                    r"frame-ancestors\s+[^;]+",
                    "frame-ancestors 'self' https://huggingface.co https://*.hf.space",
                    value,
                    flags=re.IGNORECASE,
                )
            else:
                value = f"{value}; frame-ancestors 'self' https://huggingface.co https://*.hf.space;"
        if lk == "location":
            value = fix_location_header(value, default_prefix=default_prefix)
        raw_headers.append((key.lower().encode("latin-1"), value.encode("latin-1")))

    if not has_csp:
        raw_headers.append((b"content-security-policy", b"frame-ancestors 'self' https://huggingface.co https://*.hf.space;"))

    if not has_cors:
        raw_headers.append((b"access-control-allow-origin", b"*"))
        raw_headers.append((b"access-control-allow-methods", b"GET, POST, PUT, DELETE, PATCH, OPTIONS"))
        raw_headers.append((b"access-control-allow-headers", b"*"))

    return raw_headers


async def proxy_http_request(
    target_url: str,
    request: Request,
    default_prefix: str = "",
    extra_headers: Optional[Dict[str, str]] = None,
    html_fixup: Optional[Callable[[str], str]] = None,
    body_override: Optional[bytes] = None,
) -> Response:
    client = get_http_client()
    headers = build_upstream_headers(request, extra_headers)
    body    = body_override if body_override is not None else await request.body()
    method  = request.method
    params  = dict(request.query_params)

    resp: Optional[httpx.Response] = None
    last_exc: Optional[Exception] = None

    for attempt in range(1, 4):
        try:
            req = client.build_request(
                method=method,
                url=target_url,
                headers=headers,
                params=params,
                content=body,
            )
            resp = await client.send(req, stream=True)
            if resp.status_code in (502, 503) and attempt < 3:
                await resp.aclose()
                await asyncio.sleep(0.1 * attempt)
                continue
            break
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError, httpx.HTTPError) as exc:
            last_exc = exc
            if attempt < 3:
                await asyncio.sleep(0.3 * attempt)
                continue
            if method in ("GET", "HEAD") and ("html" in request.headers.get("accept", "").lower() or request.url.path in ("/", "/index.html", "/healthz", "/health", "/omniroute", "/dashboard", "/jellyfin", "/hermes") or request.url.path.startswith("/dashboard") or request.url.path.startswith("/omniroute") or request.url.path.startswith("/jellyfin") or request.url.path.startswith("/hermes")):
                service_name = "OmniRoute AI Gateway" if default_prefix == "/omniroute" or "omniroute" in request.url.path or "dashboard" in request.url.path else ("Jellyfin Media Server" if default_prefix == "/jellyfin" or "jellyfin" in request.url.path else ("Hermes Autonomous Agent" if default_prefix == "/hermes" or "hermes" in request.url.path else "OpenCode Space"))

                html_retry = f"""<!DOCTYPE html>
<html>
<head>
    <meta http-equiv="refresh" content="3">
    <title>{service_name} Initializing...</title>

    <style>
        body {{ font-family: system-ui, -apple-system, sans-serif; background: #0d1117; color: #c9d1d9; display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; padding: 20px; box-sizing: border-box; }}
        .card {{ text-align: center; background: #161b22; padding: 2.5rem; border-radius: 12px; border: 1px solid #30363d; max-width: 500px; width: 100%; }}
        .spinner {{ width: 40px; height: 40px; border: 4px solid #30363d; border-top-color: #58a6ff; border-radius: 50%; animation: spin 1s linear infinite; margin: 0 auto 1.5rem; }}
        @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
        h2 {{ margin: 0 0 0.5rem; color: #f0f6fc; font-size: 1.25rem; }}
        p {{ color: #8b949e; font-size: 0.9rem; margin: 0; }}
    </style>
</head>
<body>
    <div class="card">
        <div class="spinner"></div>
        <h2>Initializing {service_name}...</h2>
        <p>The service is completing backend startup. This page will refresh automatically in 3 seconds.</p>
    </div>
</body>
</html>"""
                return Response(content=html_retry, status_code=200, headers={"Retry-After": "3", "Refresh": "3"}, media_type="text/html")

            return Response(
                content=f"<h2>502 Service Unavailable</h2><p>Service initializing. Please retry in a few seconds.</p>",
                status_code=502,
                media_type="text/html",
            )

    if resp is None:
        return Response(content=f"<h2>502 Bad Gateway</h2><p>{last_exc}</p>", status_code=502, media_type="text/html")

    raw_headers = build_downstream_raw_headers(resp.headers, default_prefix=default_prefix)
    content_type = resp.headers.get("content-type", "")
    status_code  = resp.status_code

    is_stream = "text/event-stream" in content_type or "video/" in content_type or "audio/" in content_type or resp.headers.get("transfer-encoding") == "chunked"

    if is_stream:
        async def stream_generator():
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
            finally:
                await resp.aclose()

        streaming_resp = StreamingResponse(
            stream_generator(),
            status_code=status_code,
            media_type=content_type or None,
        )
        streaming_resp.raw_headers = raw_headers
        return streaming_resp

    try:
        content = await resp.aread()
    finally:
        await resp.aclose()

    if "text/html" in content_type and html_fixup and content:
        try:
            text = content.decode("utf-8", errors="replace")
            text = html_fixup(text)
            content = text.encode("utf-8")
        except Exception:
            pass

    normal_resp = Response(
        content=content,
        status_code=status_code,
        media_type=content_type or None,
    )
    normal_resp.raw_headers = raw_headers
    return normal_resp


async def proxy_websocket_stream(websocket: WebSocket, target_ws_url: str):
    await websocket.accept()
    
    # Preserve query parameters from client WebSocket request (e.g. ?EIO=4&transport=websocket)
    query_string = websocket.scope.get("query_string", b"").decode("utf-8")
    if query_string:
        sep = "&" if "?" in target_ws_url else "?"
        target_ws_url = f"{target_ws_url}{sep}{query_string}"

    skip_headers = {"host", "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions"}
    forward_headers = {k: v for k, v in websocket.headers.items() if k.lower() not in skip_headers}
    forward_headers["Host"] = PUBLIC_HOST
    forward_headers["X-Forwarded-Host"] = PUBLIC_HOST
    forward_headers["X-Forwarded-Proto"] = "https"
    forward_headers["X-Forwarded-Port"] = "443"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(target_ws_url, headers=forward_headers) as upstream_ws:
                async def downstream_to_upstream():
                    async for msg in upstream_ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await websocket.send_text(msg.data)
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            await websocket.send_bytes(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                            break

                async def upstream_to_downstream():
                    while True:
                        try:
                            msg = await websocket.receive()
                            if "text" in msg:
                                await upstream_ws.send_str(msg["text"])
                            elif "bytes" in msg:
                                await upstream_ws.send_bytes(msg["bytes"])
                            elif msg.get("type") == "websocket.disconnect":
                                break
                        except Exception:
                            break

                await asyncio.gather(downstream_to_upstream(), upstream_to_downstream(), return_exceptions=True)
    except Exception as exc:
        logger.warning(f"WebSocket Proxy Exception for {target_ws_url}: {exc}")
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
