"""
Apex Ops Router
===============
Admin-gated endpoints to inspect and manage the persistent `/data` volume
(Jellyfin folder included) from outside the space container.

Authentication:
  All endpoints require `Authorization: Bearer <secret>` where <secret> matches
  the space's `APEX_OPS_KEY` (falling back to `APEX_SECRET_KEY` or `HF_TOKEN`).
  Without it every route returns 401.
"""

import os
import hmac
import shutil
import asyncio
import logging
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("ApexOps")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] [%(name)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

router = APIRouter(tags=["Ops"])

JELLYFIN_ROOT = "/data/jellyfin"
APEX_ROOT = "/data/apex"

INITIAL_DIRS = [
    os.path.join(JELLYFIN_ROOT, "data"),
    os.path.join(JELLYFIN_ROOT, "config"),
    os.path.join(JELLYFIN_ROOT, "cache"),
    os.path.join(JELLYFIN_ROOT, "log"),
    os.path.join(JELLYFIN_ROOT, "media", "Movies"),
    os.path.join(JELLYFIN_ROOT, "media", "TV Shows"),
    os.path.join(APEX_ROOT, "session"),
    os.path.join(APEX_ROOT, "backups"),
]


def _is_admin(request: Request) -> bool:
    secret = (
        os.environ.get("APEX_OPS_KEY")
        or os.environ.get("APEX_SECRET_KEY")
        or os.environ.get("HF_TOKEN")
    )
    if not secret:
        return False
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return False
    provided = auth[len("Bearer "):].strip()
    return hmac.compare_digest(provided, secret)


def _denied():
    return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)


def _build_tree(path: str, max_depth: int = 3, max_entries: int = 600):
    """Return a bounded, serializable directory tree below `path`."""
    result = {"path": path, "exists": os.path.exists(path)}

    def walk(current: str, depth: int, budget: list):
        node = {"path": current, "type": "dir"}
        try:
            names = sorted(os.listdir(current))
        except OSError as exc:
            node["error"] = str(exc)
            return node
        children = []
        for name in names:
            if budget[0] <= 0:
                children.append({"path": os.path.join(current, name), "type": "dir", "truncated": True})
                break
            budget[0] -= 1
            entry = os.path.join(current, name)
            try:
                if os.path.isdir(entry) and not os.path.islink(entry):
                    if depth < max_depth:
                        children.append(walk(entry, depth + 1, budget))
                    else:
                        children.append({"path": entry, "type": "dir"})
                else:
                    children.append({"path": entry, "type": "file", "size": os.path.getsize(entry)})
            except OSError as exc:
                children.append({"path": entry, "type": "error", "message": str(exc)})
        node["children"] = children
        return node

    if os.path.isdir(path):
        result["tree"] = walk(path, 0, [max_entries])
    else:
        result["tree"] = None
    return result


@router.get("/apex/ops/tree")
async def ops_tree(path: str = "/data", request: Request = None):
    if not _is_admin(request):
        return _denied()
    return JSONResponse(_build_tree(path))


@router.post("/apex/ops/jellyfin/reset")
async def ops_jellyfin_reset(request: Request):
    """Remove everything under /data/jellyfin and lay down a clean directory skeleton."""
    if not _is_admin(request):
        return _denied()

    removed = {"dirs": [], "files": [], "bytes": 0}
    if os.path.exists(JELLYFIN_ROOT):
        ordered = sorted(
            os.listdir(JELLYFIN_ROOT),
            key=lambda n: os.path.join(JELLYFIN_ROOT, n).count(os.sep),
            reverse=True,
        )
        for name in ordered:
            entry = os.path.join(JELLYFIN_ROOT, name)
            try:
                if os.path.isdir(entry) and not os.path.islink(entry):
                    size = 0
                    for root, _, files in os.walk(entry):
                        for f in files:
                            fp = os.path.join(root, f)
                            try:
                                size += os.path.getsize(fp)
                            except OSError:
                                pass
                    shutil.rmtree(entry)
                    removed["dirs"].append(name)
                    removed["bytes"] += size
                else:
                    try:
                        removed["bytes"] += os.path.getsize(entry)
                    except OSError:
                        pass
                    os.remove(entry)
                    removed["files"].append(name)
            except OSError as exc:
                return JSONResponse(
                    {"ok": False, "error": f"Failed to remove {entry}: {exc}", "removed": removed},
                    status_code=500,
                )

    os.makedirs("/data", exist_ok=True)
    for d in INITIAL_DIRS:
        os.makedirs(d, exist_ok=True)

    return JSONResponse({
        "ok": True,
        "jellyfin_root": JELLYFIN_ROOT,
        "removed": removed,
        "initialized": INITIAL_DIRS,
    })


@router.post("/apex/ops/jellyfin/init")
async def ops_jellyfin_init(request: Request):
    """Idempotently create the standard Jellyfin + Apex data directory skeleton."""
    if not _is_admin(request):
        return _denied()
    os.makedirs("/data", exist_ok=True)
    created = []
    for d in INITIAL_DIRS:
        if not os.path.exists(d):
            os.makedirs(d, exist_ok=True)
            created.append(d)
    return JSONResponse({"ok": True, "created": created, "base": INITIAL_DIRS})


@router.post("/apex/ops/jellyfin/scan")
async def ops_jellyfin_scan(request: Request):
    """Trigger a Jellyfin library refresh once the media server is up."""
    if not _is_admin(request):
        return _denied()
    import aiohttp

    async with aiohttp.ClientSession() as session:
        for attempt in range(6):
            if attempt:
                logger.info(f"Retrying Jellyfin scan (attempt {attempt + 1})...")
            try:
                async with session.post("http://127.0.0.1:8096/Library/Refresh", timeout=10) as resp:
                    return JSONResponse({"ok": True, "status": resp.status})
            except Exception:
                await asyncio.sleep(5)
    return JSONResponse({"ok": False, "error": "Jellyfin not reachable after retries"}, status_code=502)