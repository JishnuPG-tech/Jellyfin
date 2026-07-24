"""Reverse-proxy helpers used by `main.py`.

We keep proxy code in its own module so `main.py` stays readable and so
unit tests can exercise it directly.
"""

from __future__ import annotations

import base64
import os
from typing import Any

import httpx
from fastapi import HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, StreamingResponse


OPENCODE_HOST = os.environ.get("OPENCODE_HOST", "127.0.0.1")
OPENCODE_PORT = int(os.environ.get("OPENCODE_PORT", "4096"))
OPENCODE_BASE_URL = f"http://{OPENCODE_HOST}:{OPENCODE_PORT}"

# Same default fallback as the opencode binary: `opencode:password`.
_OPENCODE_USERNAME = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
_OPENCODE_PASSWORD = os.environ.get("OPENCODE_SERVER_PASSWORD", "password")
AUTH_HEADER = "Basic " + base64.b64encode(
    f"{_OPENCODE_USERNAME}:{_OPENCODE_PASSWORD}".encode()
).decode()


def clear_host_header(headers: dict[str, str]) -> dict[str, str]:
    h = {k: v for k, v in headers.items() if k.lower() != "host"}
    h["authorization"] = AUTH_HEADER
    return h


async def proxy_http(request: Request, path: str, timeout: float = 120.0) -> StreamingResponse | JSONResponse:
    """Forward an HTTP request to `opencode serve` and stream the response back."""
    headers = clear_host_header(dict(request.headers))
    url = f"{OPENCODE_BASE_URL}/{path}"
    if request.url.query:
        url += f"?{request.url.query}"

    body = await request.body()
    if not path.startswith("session/") and request.method == "GET" and not request.url.query:
        # Optimisation: let HTTPX stream without buffering the giant body.
        pass

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        try:
            upstream = await client.request(
                method=request.method,
                url=url,
                headers=headers,
                content=body,
            )
        except httpx.ConnectError as exc:
            raise HTTPException(status_code=502, detail=f"opencode not reachable: {exc}") from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"opencode proxy error: {exc}") from exc

        content_type = upstream.headers.get("content-type", "") or "application/octet-stream"
        resp_headers = dict(upstream.headers)
        resp_headers.pop("content-length", None)
        resp_headers.pop("content-encoding", None)

        if "text/event-stream" in content_type or "stream" in content_type:
            # Streaming path — yield bytes as they arrive (used by SSE /event_stream).
            async def stream_iter():
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            return StreamingResponse(
                stream_iter(),
                status_code=upstream.status_code,
                headers=resp_headers,
                media_type=content_type,
            )

        # Plain response — buffer it (it’s small for most endpoints).
        return StreamingResponse(
            iter([upstream.content]),
            status_code=upstream.status_code,
            headers=resp_headers,
            media_type=content_type,
        )
