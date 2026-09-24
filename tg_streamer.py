import os
import sys
import re
import json
import glob
import logging
import asyncio
import socket
import aiohttp
from aiohttp import web

# Pyrogram imports for MTProto direct chunk streaming
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import FloodWait, RPCError

logger = logging.getLogger("TG_Drive_Streamer")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] [%(name)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

HOST = "127.0.0.1"
PORT = 8080

API_ID = os.environ.get("TG_API_ID")
API_HASH = os.environ.get("TG_API_HASH")
BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
RAW_CHANNEL_ID = os.environ.get("TG_CHANNEL_ID", "")
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "b3901b0f5b9d332d7abfb9ae9e2d31f0") # Public fallback TMDB key for posters

DATA_DIR = "/data/jellyfin"
MOVIES_DIR = os.path.join(DATA_DIR, "media/Movies")
SHOWS_DIR = os.path.join(DATA_DIR, "media/TV Shows")
CACHE_FILE = os.path.join(DATA_DIR, "file_ids.json")
CONFIG_FILE = os.path.join(DATA_DIR, "channel_config.json")

os.makedirs(MOVIES_DIR, exist_ok=True)
os.makedirs(SHOWS_DIR, exist_ok=True)

# Persistent mapping: message_id -> {file_id, chat_id, file_size, title, is_tv, show_name, season, episode}
FILE_ID_CACHE = {}
DETECTED_CHANNEL_ID = None

def load_cache():
    global FILE_ID_CACHE, DETECTED_CHANNEL_ID
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                FILE_ID_CACHE = json.load(f)
                logger.info(f"[CACHE] Loaded {len(FILE_ID_CACHE)} file_ids from disk cache.")
        except Exception as e:
            logger.warning(f"[CACHE] Error loading file_ids.json: {e}")
            
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                cfg = json.load(f)
                DETECTED_CHANNEL_ID = cfg.get("channel_id")
                logger.info(f"[CONFIG] Loaded detected channel_id: {DETECTED_CHANNEL_ID}")
        except Exception as e:
            pass

def _save_cache_sync():
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(FILE_ID_CACHE, f, indent=2)
    except Exception as e:
        logger.warning(f"[CACHE] Error saving file_ids.json: {e}")

async def save_cache_async():
    await asyncio.to_thread(_save_cache_sync)

def save_cache():
    _save_cache_sync()

load_cache()

# Persistent Pyrogram Bot Client
tg_app = None
if API_ID and API_HASH and BOT_TOKEN:
    try:
        tg_app = Client(
            "tg_jellyfin_session",
            api_id=int(API_ID),
            api_hash=API_HASH,
            bot_token=BOT_TOKEN,
            workdir=DATA_DIR,
            in_memory=False
        )
        logger.info("[PYROGRAM] Pyrogram persistent client initialized.")
    except Exception as e:
        logger.error(f"[PYROGRAM] Error initializing Pyrogram: {e}")

routes = web.RouteTableDef()

@routes.get("/")
@routes.get("/health")
async def health(request):
    is_ready = bool(tg_app and tg_app.is_connected)
    return web.json_response({
        "status": "ok",
        "service": "TG-Drive High-Speed 5G Streamer",
        "pyrogram_connected": is_ready,
        "cached_files": len(FILE_ID_CACHE),
        "movies_dir": MOVIES_DIR,
        "shows_dir": SHOWS_DIR
    })

def clean_title_str(text):
    """Clean raw filename/caption into a clean title"""
    if not text:
        return None
    text = re.sub(r'\.(mp4|mkv|avi|mov|wmv|flv)$', '', text, flags=re.IGNORECASE)
    text = text.replace('.', ' ').replace('_', ' ')
    return text.strip()

def parse_media_type(filename_or_caption):
    """
    Detects if media is a TV Show episode or Movie.
    Returns: (is_tv, title, show_name, season_num, episode_num)
    """
    clean_text = clean_title_str(filename_or_caption) or "Unknown_Media"
    
    pattern_s_e = re.search(r'(?i)(.*?)\b[S|season]\s*(\d{1,2})\s*[E|ep|episode]\s*(\d{1,2})\b', clean_text)
    if pattern_s_e:
        show_name = pattern_s_e.group(1).strip()
        season = int(pattern_s_e.group(2))
        episode = int(pattern_s_e.group(3))
        title = f"{show_name} - S{season:02d}E{episode:02d}"
        return True, title, show_name, season, episode

    pattern_ep = re.search(r'(?i)(.*?)\b(?:ep|episode)\s*(\d{1,3})\b', clean_text)
    if pattern_ep:
        show_name = pattern_ep.group(1).strip()
        season = 1
        episode = int(pattern_ep.group(2))
        title = f"{show_name} - S{season:02d}E{episode:02d}"
        return True, title, show_name, season, episode

    return False, clean_text, clean_text, None, None

async def fetch_tmdb_poster(title, target_dir, filename_prefix):
    """Fetches high-resolution movie/show poster from TMDB and saves poster.jpg"""
    if not TMDB_API_KEY or not title:
        return
    try:
        search_title = re.sub(r'\b(19|20)\d{2}\b', '', title).strip()
        url = f"https://api.themoviedb.org/3/search/multi?api_key={TMDB_API_KEY}&query={search_title}"
        connector = aiohttp.TCPConnector(family=socket.AF_INET)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(url, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    results = data.get("results", [])
                    if results:
                        poster_path = results[0].get("poster_path")
                        if poster_path:
                            img_url = f"https://image.tmdb.org/t/p/w500{poster_path}"
                            async with session.get(img_url) as img_resp:
                                if img_resp.status == 200:
                                    img_data = await img_resp.read()
                                    save_path = os.path.join(target_dir, f"{filename_prefix}-poster.jpg")
                                    with open(save_path, "wb") as f:
                                        f.write(img_data)
                                    logger.info(f"[TMDB] 🖼️ Saved poster image: {save_path}")
    except Exception as e:
        logger.warning(f"[TMDB] Poster fetch notice for '{title}': {e}")

def _create_strm_file_sync(msg_id, file_id, clean_title, is_tv=False, show_name=None, season=None, episode=None):
    if is_tv and show_name:
        season_num = season if season else 1
        target_dir = os.path.join(SHOWS_DIR, show_name, f"Season {season_num:02d}")
        os.makedirs(target_dir, exist_ok=True)
        strm_filename = f"{clean_title}.strm"
    else:
        target_dir = MOVIES_DIR
        strm_filename = f"{clean_title}.strm"

    strm_path = os.path.join(target_dir, strm_filename)
    stream_url = f"http://127.0.0.1:8080/stream_file?file_id={file_id}&message_id={msg_id}&filename={clean_title}.mp4"

    with open(strm_path, "w") as f:
        f.write(stream_url)

    logger.info(f"[AUTO-SYNC] 🎉 Created .strm file: {strm_filename} -> {strm_path}")
    return strm_filename, target_dir

async def create_strm_file_async(msg_id, file_id, clean_title, is_tv=False, show_name=None, season=None, episode=None):
    strm_filename, target_dir = await asyncio.to_thread(_create_strm_file_sync, msg_id, file_id, clean_title, is_tv, show_name, season, episode)
    asyncio.create_task(fetch_tmdb_poster(show_name if is_tv else clean_title, target_dir, clean_title))
    return strm_filename

def create_strm_file(msg_id, file_id, clean_title, is_tv=False, show_name=None, season=None, episode=None):
    strm_filename, target_dir = _create_strm_file_sync(msg_id, file_id, clean_title, is_tv, show_name, season, episode)
    try:
        asyncio.create_task(fetch_tmdb_poster(show_name if is_tv else clean_title, target_dir, clean_title))
    except Exception:
        pass
    return strm_filename

async def trigger_jellyfin_scan():
    """Trigger Jellyfin Library Scan automatically with retry while Jellyfin boots"""
    for attempt in range(5):
        try:
            connector = aiohttp.TCPConnector(family=socket.AF_INET)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.post("http://127.0.0.1:8096/Library/Refresh") as resp:
                    logger.info(f"Jellyfin library refresh triggered: {resp.status}")
                    return
        except Exception as e:
            if attempt < 4:
                await asyncio.sleep(3)
                continue
            logger.warning(f"Could not trigger Jellyfin library refresh after retries: {e}")

WEBHOOK_SECRET = os.environ.get("TG_WEBHOOK_SECRET")

@routes.post("/")
@routes.post("/telegram-webhook")
@routes.post("/webhook")
async def telegram_webhook(request):
    try:
        if WEBHOOK_SECRET:
            token = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
            if token != WEBHOOK_SECRET:
                return web.json_response({"ok": False, "error": "Unauthorized"}, status=401)

        data = await request.json()
        post = data.get("channel_post") or data.get("message")
        if not post:
            return web.json_response({"ok": True})

        msg_id = post.get("message_id")
        chat = post.get("chat", {})
        chat_id = chat.get("id")

        media_obj = post.get("video") or post.get("document") or post.get("audio") or post.get("animation")
        if not media_obj:
            return web.json_response({"ok": True})

        file_id = media_obj.get("file_id")
        file_name = media_obj.get("file_name") or post.get("caption") or f"Telegram_Media_{msg_id}"
        file_size = media_obj.get("file_size", 0)

        is_tv, title, show_name, season, episode = parse_media_type(file_name)

        if file_id and msg_id:
            FILE_ID_CACHE[str(msg_id)] = {
                "file_id": file_id,
                "chat_id": chat_id,
                "file_size": file_size,
                "title": title,
                "is_tv": is_tv,
                "show_name": show_name,
                "season": season,
                "episode": episode
            }
            await save_cache_async()

            strm_name = await create_strm_file_async(msg_id, file_id, title, is_tv, show_name, season, episode)
            logger.info(f"[WEBHOOK] 🎉 Successfully indexed media from Webhook: {strm_name}")
            await trigger_jellyfin_scan()

        return web.json_response({"ok": True})
    except Exception as e:
        logger.error(f"[WEBHOOK] Error processing webhook payload: {e}")
        return web.json_response({"ok": True})

@routes.get("/stream_file")
@routes.get("/stream/{message_id}")
@routes.get("/stream/{message_id}/{filename}")
async def stream_file(request):
    """
    High-Speed 5G Byte-Accurate Range Streamer:
    Optimized TCP Keep-Alive, memory caching headers, and multi-chunk Pyrogram MTProto buffer.
    Pushes maximum throughput to Jellyfin / mobile clients without CPU re-encoding overhead!
    """
    file_id = request.query.get("file_id")
    msg_id_str = request.match_info.get("message_id") or request.query.get("message_id")
    filename = request.match_info.get("filename", "video.mp4")

    cached_entry = FILE_ID_CACHE.get(str(msg_id_str))
    file_size = 0
    if isinstance(cached_entry, dict):
        if not file_id:
            file_id = cached_entry.get("file_id")
        file_size = cached_entry.get("file_size", 0)

    if not file_id:
        return web.Response(status=404, text=f"Media not available for message {msg_id_str}.")

    if tg_app and tg_app.is_connected:
        try:
            range_header = request.headers.get("Range")
            start = 0
            end = file_size - 1 if file_size > 0 else 0

            if range_header:
                match = re.match(r"^bytes=(\d+)-(\d+)?$", range_header)
                if match:
                    start = int(match.group(1))
                    if match.group(2):
                        end = int(match.group(2))
                    elif file_size > 0:
                        end = file_size - 1

            if file_size > 0 and end >= file_size:
                end = file_size - 1

            req_length = (end - start + 1) if (file_size > 0 and end >= start) else file_size

            # Pyrogram 1MB Chunk calculation
            CHUNK_SIZE = 1024 * 1024
            start_chunk = start // CHUNK_SIZE
            skip_leading_bytes = start % CHUNK_SIZE
            chunk_count = ((end - start + skip_leading_bytes + CHUNK_SIZE) // CHUNK_SIZE) if file_size > 0 else 0

            status = 206 if range_header and file_size > 0 else 200

            # High-Performance 5G & Direct Play Headers
            headers = {
                "Content-Type": "video/mp4",
                "Accept-Ranges": "bytes",
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "public, max-age=86400",
                "Connection": "keep-alive",
                "Content-Disposition": f'inline; filename="{filename}"'
            }
            if file_size > 0:
                headers["Content-Length"] = str(req_length)
                if range_header:
                    headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

            response = web.StreamResponse(status=status, headers=headers)
            await response.prepare(request)

            bytes_written = 0
            is_first_chunk = True

            try:
                async for chunk in tg_app.stream_media(file_id, offset=start_chunk, limit=chunk_count):
                    if is_first_chunk:
                        chunk = chunk[skip_leading_bytes:]
                        is_first_chunk = False

                    if bytes_written + len(chunk) > req_length:
                        needed = req_length - bytes_written
                        await response.write(chunk[:needed])
                        bytes_written += needed
                        break
                    else:
                        await response.write(chunk)
                        bytes_written += len(chunk)
                        if bytes_written >= req_length:
                            break
            except (ConnectionResetError, asyncio.CancelledError):
                pass

            return response
        except Exception as e:
            if not isinstance(e, (ConnectionResetError, asyncio.CancelledError)):
                logger.warning(f"[STREAM] MTProto stream exception: {e}")

    return web.Response(status=500, text="Streaming temporarily unavailable.")

async def restore_cached_strm_files():
    """Restores all cached movie & TV show .strm files on boot"""
    count = 0
    for msg_id, data in FILE_ID_CACHE.items():
        if isinstance(data, dict):
            file_id = data.get("file_id")
            title = data.get("title") or f"Media_{msg_id}"
            is_tv = data.get("is_tv", False)
            show_name = data.get("show_name")
            season = data.get("season")
            episode = data.get("episode")

            if file_id and title:
                await create_strm_file_async(msg_id, file_id, title, is_tv, show_name, season, episode)
                count += 1
    if count > 0:
        logger.info(f"[RESTORE] Restored {count} .strm file(s) from persistent disk cache.")
        await trigger_jellyfin_scan()

async def start_pyrogram():
    """Starts Pyrogram Client and restores cached media"""
    await restore_cached_strm_files()

    if not tg_app:
        logger.warning("[PYROGRAM] Pyrogram client not configured.")
        return

    logger.info("[PYROGRAM] Starting Pyrogram MTProto Client...")
    await tg_app.start()
    logger.info("[PYROGRAM] Pyrogram Client started successfully!")

async def stop_pyrogram():
    if tg_app and tg_app.is_connected:
        await tg_app.stop()

async def start_background_tasks(app):
    app['pyrogram_task'] = asyncio.create_task(start_pyrogram())

async def cleanup_background_tasks(app):
    await stop_pyrogram()

app = web.Application()
app.add_routes(routes)
app.on_startup.append(start_background_tasks)
app.on_cleanup.append(cleanup_background_tasks)

if __name__ == "__main__":
    logger.info(f"Starting TG-Drive High-Speed 5G Stream Proxy on {HOST}:{PORT}")
    web.run_app(app, host=HOST, port=PORT)
