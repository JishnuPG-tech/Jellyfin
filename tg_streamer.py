import os
import sys
import re
import json
import glob
import logging
import asyncio
import socket
import urllib.parse
import aiohttp
import httpx
from aiohttp import web

# Pyrogram imports for MTProto direct chunk streaming
from pyrogram import Client, filters
from pyrogram.enums import ChatType
from pyrogram.types import Message
from pyrogram.errors import FloodWait, RPCError, AuthKeyDuplicated

# Apex streaming core (telegram-free; wired to Pyrogram below)
from apex_stream import Config, BoundedChunkCache, ClientPool, InFlightRegistry, StreamMetrics, StreamDriver, FileInfo, RangeNotSatisfiable, NoClientAvailable

logger = logging.getLogger("TG_Drive_Streamer")
if not logger.handlers:
    _fmt = logging.Formatter("%(asctime)s [%(levelname)s] [%(name)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    _con = logging.StreamHandler()
    _con.setFormatter(_fmt)
    logger.addHandler(_con)
    try:
        os.makedirs("/data/cache", exist_ok=True)
        _file = logging.FileHandler("/data/cache/tg_streamer.log", encoding="utf-8")
        _file.setFormatter(_fmt)
        logger.addHandler(_file)
    except Exception as _e:
        logger.warning(f"[LOG] File logging unavailable: {_e}")
    logger.setLevel(logging.INFO)

HOST = "127.0.0.1"
PORT = int(os.environ.get("TG_LISTEN_PORT", os.environ.get("PORT", "8080")))

def get_env(*names, default=None):
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return default

API_ID = get_env("TELEGRAM_API_ID", "TG_API_ID")
API_HASH = get_env("TELEGRAM_API_HASH", "TG_API_HASH")
BOT_TOKEN = get_env("TELEGRAM_BOT_TOKEN", "TG_BOT_TOKEN")
RAW_CHANNEL_ID = get_env("TELEGRAM_ALLOWED_CHAT_IDS", "TG_CHANNEL_ID", default="")
TMDB_API_KEY = get_env("TMDB_API_KEY")

# Public URL Telegram must deliver bot updates to (through the Apex gateway).
PUBLIC_BASE_URL = get_env("PUBLIC_BASE_URL", "PUBLIC_URL", default="https://jishnupg-apex.hf.space")
WEBHOOK_PATH = "/tg-stream/telegram-webhook"

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

# ---------------------------------------------------------------------------
# Apex streaming core singletons (wired to Pyrogram clients below).
# ---------------------------------------------------------------------------
APEX_CFG = Config()
apex_cache = BoundedChunkCache(
    max_bytes=APEX_CFG.cache_size_bytes if APEX_CFG.cache_enabled_effective() else 0,
    ttl_seconds=APEX_CFG.cache_ttl_seconds,
)
apex_pool = ClientPool(
    max_streams_per_client=APEX_CFG.max_per_client,
    failure_threshold=APEX_CFG.client_failure_threshold,
    cooldown_seconds=APEX_CFG.client_cooldown_seconds,
    logger=logger,
)
apex_registry = InFlightRegistry(
    cache=apex_cache,
    run_size=APEX_CFG.run_size,
    max_window=APEX_CFG.run_window,
    max_inflight_runs=APEX_CFG.max_inflight_runs,
    logger_obj=logger,
)
apex_metrics = StreamMetrics()


async def _resolve_source(chat_id, message_id):
    """Look up a (chat_id, message_id) source in the persistent index."""
    entry = FILE_ID_CACHE.get(str(message_id))
    if not isinstance(entry, dict):
        return None
    if chat_id and entry.get("chat_id") and str(entry.get("chat_id")) != str(chat_id):
        return None
    return FileInfo(
        size=int(entry.get("file_size") or 0),
        name=entry.get("title") or f"Media_{message_id}",
        file_id=entry.get("file_id"),
        mime_type="video/mp4",
    )


async def _fetch_chunks(client, chat_id, message_id, run_start, chunk_count):
    """Yield (offset_in_run, chunk) 1 MiB chunks from one client via MTProto."""
    entry = FILE_ID_CACHE.get(str(message_id))
    if not isinstance(entry, dict):
        return
    file_id = entry.get("file_id")
    if not file_id:
        return
    i = 0
    async for chunk in client.stream_media(file_id, offset=run_start, limit=chunk_count):
        yield (i, chunk)
        i += 1


apex_driver = StreamDriver(
    cfg=APEX_CFG,
    pool=apex_pool,
    registry=apex_registry,
    metrics=apex_metrics,
    resolve_fn=_resolve_source,
    fetch_fn=_fetch_chunks,
    logger_obj=logger,
)

# Extra Telegram bots (APEX_TELEGRAM_EXTRA_TOKENS): started solely for streaming;
# they never consume updates so the webhook/primary bot owns ingestion.
extra_clients = []
for _ec_idx, _ec_token in enumerate(APEX_CFG.extra_tokens, start=2):
    if API_ID and API_HASH and _ec_token:
        try:
            extra_clients.append((_ec_idx, Client(
                f"tg_extra_{_ec_idx}",
                api_id=int(API_ID),
                api_hash=API_HASH,
                bot_token=_ec_token,
                workdir=DATA_DIR,
                in_memory=True,
                no_updates=True,
            )))
        except Exception as e:
            logger.error(f"[PYROGRAM] Error initializing extra client {_ec_idx}: {e}")

def index_media(msg_id, chat_id, file_id, file_size, file_name):
    """Core ingestion: cache the file and create a .strm for Jellyfin."""
    if not file_id or not msg_id:
        return False

    cached = FILE_ID_CACHE.get(str(msg_id))
    if isinstance(cached, dict) and cached.get("file_id") == file_id:
        return cached.get("title") or True

    is_tv, title, show_name, season, episode = parse_media_type(file_name)

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
    save_cache()

    strm_name = create_strm_file(msg_id, file_id, title, is_tv, show_name, season, episode)
    logger.info(f"[INGEST] Media indexed from Telegram: {strm_name} (chat={chat_id})")
    return strm_name


def media_file_name(message, media):
    return getattr(media, "file_name", None) or message.caption or f"Telegram_Media_{message.id}"


def allowed_chat(chat) -> bool:
    if not RAW_CHANNEL_ID:
        return True
    allowed = [a.strip() for a in RAW_CHANNEL_ID.split(",") if a.strip()]
    if not allowed:
        return True
    if chat and chat.type == ChatType.PRIVATE:
        return True
    return str(chat.id) in allowed


async def process_telegram_media(message, is_channel_post):
    """Shared media handler for Pyrogram DM + channel post updates."""
    try:
        media = message.video or message.document or message.audio or message.animation
        if not media or not getattr(media, "file_id", None):
            return
        chat = message.chat
        chat_id = chat.id if chat else None
        if chat_id is None or not allowed_chat(chat):
            logger.info(f"[PYROGRAM] Skipped media from chat {chat_id} (type={getattr(chat, 'type', '?')}) - not in allowed list")
            return

        if is_channel_post:
            global DETECTED_CHANNEL_ID
            DETECTED_CHANNEL_ID = chat_id
            try:
                with open(CONFIG_FILE, "w") as f:
                    json.dump({"channel_id": chat_id}, f)
            except Exception as e:
                logger.warning(f"[CONFIG] Could not persist channel_id: {e}")

        strm_name = index_media(message.id, chat_id, media.file_id, media.file_size or 0,
                                media_file_name(message, media))
        if strm_name:
            logger.info(f"[PYROGRAM] 🎉 Ingested '{strm_name}' from chat {chat_id}")
            await trigger_jellyfin_scan()
    except Exception as e:
        logger.error(f"[PYROGRAM] Error handling update: {e}")


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
            in_memory=True
        )
        logger.info("[PYROGRAM] Pyrogram persistent client initialized.")
    except Exception as e:
        logger.error(f"[PYROGRAM] Error initializing Pyrogram: {e}")

if tg_app:
    @tg_app.on_message()
    async def on_any_update(client, message):
        chat = message.chat
        chat_t = getattr(chat, "type", None)
        logger.info(f"[PYROGRAM][RX] Received update: chat={chat.id if chat else '?'} type={chat_t} media={getattr(message, 'media', None)} caption={str(message.caption or '')[:40]!r}")

    @tg_app.on_message(filters.video | filters.document | filters.audio | filters.animation)
    async def on_media_message(client, message):
        is_channel_post = bool(message.chat and message.chat.type == ChatType.CHANNEL)
        await process_telegram_media(message, is_channel_post)
    logger.info("[PYROGRAM] Message & channel-post media handlers registered.")

routes = web.RouteTableDef()

@routes.get("/")
@routes.get("/health")
async def health(request):
    pool_state = apex_pool.snapshot()
    is_ready = bool(tg_app and tg_app.is_connected)
    return web.json_response({
        "status": "ok",
        "service": "TG-Drive High-Speed 5G Streamer",
        "pyrogram_configured": bool(tg_app),
        "pyrogram_connected": is_ready,
        "pool_clients": pool_state["total"],
        "pool": pool_state["clients"],
        "active_runs": apex_registry.active_run_count(),
        "inflight_runs": apex_registry.snapshot(),
        "cache": apex_cache.stats(),
        "metrics": {k: v for k, v in apex_metrics.snapshot().items() if k != "recent_streams"},
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
    
    pattern_s_e = re.search(r'(?i)(.*?)\b(?:S|Season)\s*(\d{1,2})\s*(?:E|Ep|Episode)\s*(\d{1,2})\b', clean_text)
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
    """Trigger Jellyfin Library Scan automatically with retry while Jellyfin boots."""
    headers = _jellyfin_auth_header()
    for attempt in range(8):
        try:
            connector = aiohttp.TCPConnector(family=socket.AF_INET)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.post("http://127.0.0.1:8096/Library/Refresh", headers=headers, timeout=15) as resp:
                    logger.info(f"Jellyfin library refresh triggered: {resp.status}")
                    if resp.status in (200, 204):
                        return
        except Exception as e:
            if attempt < 7:
                await asyncio.sleep(5)
                continue
            logger.warning(f"Could not trigger Jellyfin library refresh after retries: {e}")

WEBHOOK_SECRET = get_env("APEX_WEBHOOK_SECRET", "TG_WEBHOOK_SECRET")

def _read_jellyfin_api_key():
    """Return a working Jellyfin API key: env secret first, then any active row from the DB."""
    key = get_env("APEX_JELLYFIN_API_KEY", "JELLYFIN_API_KEY", default="").strip().strip('"')
    if key:
        return key
    for db in ("/opt/jellyfin-local/data/data/jellyfin.db", "/data/jellyfin/data/data/jellyfin.db"):
        if os.path.exists(db):
            try:
                with __import__("sqlite3").connect(db, timeout=5) as conn:
                    cur = conn.cursor()
                    cur.execute("PRAGMA table_info('api_keys')")
                    columns = [r[1] for r in cur.fetchall()]
                    if "AccessToken" not in columns:
                        continue
                    if "IsActive" in columns:
                        cur.execute("SELECT AccessToken FROM api_keys WHERE IsActive IS NOT 0 ORDER BY DateCreated DESC LIMIT 1")
                    else:
                        cur.execute("SELECT AccessToken FROM api_keys ORDER BY DateCreated DESC LIMIT 1")
                    row = cur.fetchone()
                    if row and row[0]:
                        return str(row[0]).strip()
            except Exception as e:
                logger.warning(f"[JELLYFIN] Could not read api_keys table from {db}: {e}")
    return ""


def _jellyfin_auth_header():
    """Build a full-format MediaBrowser Authorization header accepted by Jellyfin 12+."""
    key = _read_jellyfin_api_key()
    if not key:
        return {}
    return {
        "Authorization": (
            'MediaBrowser Client="TG-Drive Streamer", '
            'Device="Apex Server", '
            'DeviceId="tg-streamer", '
            'Version="12.1.0", '
            f'Token="{key}"'
        ),
        "Accept": "application/json",
    }

async def telegram_api_call(method: str, **params):
    """Call the Telegram Bot API via httpx (uses trust_env to honor proxy vars)."""
    if not BOT_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    last_error = None
    async with httpx.AsyncClient(trust_env=True, timeout=20.0) as client:
        for attempt in range(3):
            try:
                resp = await client.post(url, json=params)
                try:
                    return resp.json()
                except Exception:
                    return {"ok": False, "description": f"HTTP {resp.status_code}"}
            except Exception as e:
                last_error = e
                logger.warning(f"[TELEGRAM] {method} attempt {attempt+1} failed: {type(e).__name__}: {e}")
                await asyncio.sleep(2)
    return {"ok": False, "description": f"Telegram API error: {last_error}"}


async def register_telegram_webhook():
    """Register the streamer URL as this bot's webhook so Telegram delivers updates here.

    Telegram delivers bot updates ONLY to a registered webhook or via getUpdates
    (long-polling). Pyrogram MTProto also needs the webhook absent/consistent, so we
    explicitly set the webhook to our public /tg-stream/telegram-webhook endpoint.
    """
    webhook_url = f"{PUBLIC_BASE_URL}{WEBHOOK_PATH}"
    params = {"url": webhook_url}
    if WEBHOOK_SECRET:
        params["secret_token"] = WEBHOOK_SECRET
    params["allowed_updates"] = ["message", "channel_post"]

    last_result = None
    for attempt in range(3):
        result = await telegram_api_call("setWebhook", **params)
        last_result = result
        ok = bool(result and result.get("ok"))
        desc = (result or {}).get("description", "no response")
        logger.info(f"[WEBHOOK] setWebhook attempt {attempt+1}: ok={ok} desc={desc}")
        if ok:
            return True, webhook_url, desc
        await asyncio.sleep(3)

    return False, webhook_url, str((last_result or {}).get("description", "unknown error"))


async def get_telegram_webhook_info():
    """Return current webhook registration state for this bot."""
    try:
        result = await telegram_api_call("getWebhookInfo")
        return result or {"ok": False, "description": "no response"}
    except Exception as e:
        return {"ok": False, "description": str(e)}

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
            if FILE_ID_CACHE.get(str(msg_id)):
                return web.json_response({"ok": True, "dedup": True})

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

@routes.get("/webhook-info")
async def webhook_info(request):
    info = await get_telegram_webhook_info()
    return web.json_response(info)


@routes.get("/webhook-link")
async def webhook_link(request):
    """HTML page with clickable links the user opens in their OWN browser.

    The container itself cannot reach api.telegram.org over HTTP (egress blocked),
    but the user's browser can. Generate the setWebhook/deleteWebhook URLs server-side
    (the bot token never leaves the container command output) and let the user click.
    """
    qs = urllib.parse.urlencode({
        "url": f"{PUBLIC_BASE_URL}{WEBHOOK_PATH}",
        "allowed_updates": '["message","channel_post"]',
    })
    set_url = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?{qs}" if BOT_TOKEN else ""
    del_url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook" if BOT_TOKEN else ""
    webhook_page = f"""<!DOCTYPE html>
<html><head><title>Telegram Webhook — One-Click Activation</title></head>
<body style='font-family:system-ui;background:#0f172a;color:#f8fafc;text-align:center;padding:40px;'>
<h2>Telegram Bot Webhook Activation</h2>
<p>Your container cannot reach api.telegram.org directly, so click the button below in YOUR browser to activate the connection.</p>
<p>Public webhook endpoint: <code>{PUBLIC_BASE_URL}{WEBHOOK_PATH}</code></p>
<a href='{set_url}' target='_blank'
   style='display:inline-block;margin:12px;padding:16px 28px;background:#22c55e;color:#fff;text-decoration:none;border-radius:8px;font-weight:bold;'>
   ✅ Set Webhook (activate connection)
</a>
<a href='{del_url}' target='_blank'
   style='display:inline-block;margin:12px;padding:16px 28px;background:#ef4444;color:#fff;text-decoration:none;border-radius:8px;font-weight:bold;'>
   🗑️ Delete Webhook (reset)
</a>
<p style='margin-top:24px;color:#94a3b8;'>After clicking "Set Webhook" you'll see <code>{{"ok":true}}</code> from Telegram. Then send a video/file to <b>@tgfiledrivebot</b> or post it in the configured channel.</p>
</body></html>"""
    return web.Response(text=webhook_page, content_type="text/html")


@routes.post("/register-webhook")
@routes.get("/register-webhook")
async def register_webhook_route(request):
    ok, url, desc = await register_telegram_webhook()
    return web.json_response({"ok": ok, "url": url, "description": desc, "purpose": "activate bot connection"})


def _source_path_from_request(request):
    """Return (chat_id, message_id) for a request on any supported route."""
    chat_id = request.match_info.get("chat_id")
    msg_id = request.match_info.get("message_id") or request.query.get("message_id")
    if msg_id is None:
        return None
    try:
        message_id = int(msg_id)
    except (TypeError, ValueError):
        return None
    # Legacy /stream_file and /stream/{message_id} routes carry no chat_id;
    # recover it from the index (0 = any channel is acceptable).
    if chat_id is None:
        entry = FILE_ID_CACHE.get(str(message_id))
        if isinstance(entry, dict) and entry.get("chat_id"):
            chat_id = entry["chat_id"]
        else:
            chat_id = 0
    try:
        chat_id = int(chat_id)
    except (TypeError, ValueError):
        chat_id = 0
    return chat_id, message_id


async def _wait_pool_ready():
    """Wait briefly for at least one MTProto client to register (startup grace)."""
    for _ in range(30):
        if len(apex_pool) > 0:
            return True
        await asyncio.sleep(1)
    return False


@routes.get("/stream_file")
@routes.get("/stream/{message_id}")
@routes.get("/stream/{message_id}/{filename}")
@routes.get("/stream/{chat_id}/{message_id}/video.mp4")
@routes.head("/stream_file")
@routes.head("/stream/{message_id}")
@routes.head("/stream/{message_id}/{filename}")
@routes.head("/stream/{chat_id}/{message_id}/video.mp4")
async def stream_file(request):
    """
    Coalescing, byte-accurate Range streamer backed by the apex_stream driver:
    multi-client pool, shared in-flight Telegram fetches, bounded LRU+TTL cache,
    client failover and per-request cancellation. All old routes are preserved.
    """
    source = _source_path_from_request(request)
    if source is None:
        return web.Response(status=400, text="message_id is required.")

    chat_id, message_id = source

    entry = FILE_ID_CACHE.get(str(message_id))
    filename = request.match_info.get("filename", "video.mp4")
    if not isinstance(entry, dict):
        return web.Response(status=404, text=f"Media not available for message {message_id}.")

    if len(apex_pool) == 0 and not await _wait_pool_ready():
        # Jellyfin probes .strm files at library-scan time, which can run before the
        # MTProto client has finished connecting. Wait briefly instead of failing fast
        # so ffprobe/ffmpeg get a real stream rather than a 500.
        return web.Response(status=503, text="Streaming temporarily unavailable (Telegram client not connected yet).")

    range_header = request.headers.get("Range")
    try:
        plan = await apex_driver.plan(chat_id, message_id, range_header)
    except FileNotFoundError:
        return web.Response(status=404, text=f"Media not available for message {message_id}.")
    except RangeNotSatisfiable:
        size = int(entry.get("file_size") or 0)
        headers = {"Content-Range": f"bytes */{size}"} if size > 0 else {}
        return web.Response(status=416, headers=headers, text="Requested range not satisfiable.")
    except ValueError:
        return web.Response(status=404, text="Media has no bytes.")

    headers = {
        "Content-Type": plan.content_type,
        "Accept-Ranges": plan.accept_ranges,
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "public, max-age=86400",
        "Connection": "keep-alive",
        "Content-Length": str(plan.content_length),
        "Content-Disposition": f'inline; filename="{filename}"',
    }
    if plan.content_range:
        headers["Content-Range"] = plan.content_range

    response = web.StreamResponse(status=plan.status, headers=headers)
    await response.prepare(request)

    started_at = time.monotonic()
    bytes_written = 0
    try:
        async for chunk in apex_driver.generate(plan):
            await response.write(chunk)
            bytes_written += len(chunk)
    except asyncio.CancelledError:
        apex_metrics.record_stream({
            "source": f"{chat_id}:{message_id}",
            "status": "cancelled",
            "duration": round(time.monotonic() - started_at, 3),
            "bytes_sent": bytes_written,
        })
        raise
    except ConnectionResetError:
        apex_metrics.record_stream({
            "source": f"{chat_id}:{message_id}",
            "status": "cancelled",
            "duration": round(time.monotonic() - started_at, 3),
            "bytes_sent": bytes_written,
        })
    except Exception as e:
        # A hard failure (retries exhausted) — surface it but keep the process up.
        logger.warning(f"[STREAM] generate error for {chat_id}:{message_id}: {type(e).__name__}: {e}")
        apex_metrics.record_stream({
            "source": f"{chat_id}:{message_id}",
            "status": "error",
            "error": str(e),
            "duration": round(time.monotonic() - started_at, 3),
            "bytes_sent": bytes_written,
        })
        if not response.prepared:
            return web.Response(status=502, text="Streaming source unavailable.")
    return response


@routes.get("/status")
async def status(request):
    snap = apex_driver.status_snapshot()
    snap["cached_files"] = len(FILE_ID_CACHE)
    return web.json_response(snap)


@routes.get("/metrics")
async def metrics(request):
    return web.json_response(apex_metrics.snapshot())

async def restore_cached_strm_files():
    """Removes obsolete Go-era .strm files and restores every cached movie & TV show from disk."""
    logger.info('[MIGRATION] Cleaning up old Go .strm files...')
    removed = 0
    media_dir = os.path.join(DATA_DIR, "media")
    for root, _, files in os.walk(media_dir):
        for f in files:
            if f.endswith('.strm'):
                path = os.path.join(root, f)
                try:
                    with open(path, 'r') as strm_f:
                        content = strm_f.read()
                    if '8084' in content or 'apx_' in content:
                        os.remove(path)
                        logger.info(f'Removed obsolete Go STRM: {path}')
                        removed += 1
                except Exception:
                    pass
    if removed:
        logger.info(f"[MIGRATION] Removed {removed} obsolete Go-era .strm file(s)")

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

async def _register_main_client():
    """Register the primary client in the pool once it is connected."""
    if tg_app is None:
        return
    clients = apex_pool.snapshot()["clients"]
    if not any(c["id"] == 1 for c in clients):
        await apex_pool.register(1, tg_app, ready=True)
        logger.info("[POOL] primary client registered")


async def _start_extra_clients():
    """Start extra streaming bots and register them in the pool."""
    for idx, client in extra_clients:
        try:
            if client.is_connected:
                continue
            await client.start()
            logger.info(f"[PYROGRAM] extra client {idx} started")
            await apex_pool.register(idx, client, ready=True)
            logger.info(f"[POOL] extra client {idx} registered")
        except Exception as e:
            logger.warning(f"[PYROGRAM] extra client {idx} failed to start: {type(e).__name__}: {e}")
            try:
                await client.stop()
            except Exception:
                pass


async def start_pyrogram():
    """Starts Pyrogram Client (retrying through transient session collisions) and restores cached media."""
    if not tg_app:
        logger.warning("[PYROGRAM] Pyrogram client not configured.")
        await _start_extra_clients()
        await restore_cached_strm_files()
        return

    max_attempts = 12
    session_path = os.path.join(DATA_DIR, "tg_jellyfin_session.session")

    for attempt in range(1, max_attempts + 1):
        try:
            logger.info(f"[PYROGRAM] Starting Pyrogram MTProto Client... (attempt {attempt}/{max_attempts})")
            await tg_app.start()
            me = await tg_app.get_me()
            logger.info(f"[PYROGRAM] Pyrogram Client started successfully! Bot: @{getattr(me, 'username', '?')} (id={getattr(me, 'id', '?')})")
            await _register_main_client()
            await _start_extra_clients()
            await restore_cached_strm_files()
            await register_telegram_webhook()
            return
        except AuthKeyDuplicated as e:
            logger.warning(f"[PYROGRAM] AUTH_KEY_DUPLICATED (attempt {attempt}): {e}")
            try:
                await tg_app.stop()
            except Exception:
                pass
            if attempt < max_attempts:
                await asyncio.sleep(15)
        except Exception as e:
            logger.warning(f"[PYROGRAM] start attempt {attempt} failed: {e}")
            try:
                await tg_app.stop()
            except Exception:
                pass
            if attempt < max_attempts:
                await asyncio.sleep(10)

    # Still failing after retries: the session file is likely clobbered by a stale
    # container/holdover auth key. Recreate a fresh one from the bot token.
    logger.error("[PYROGRAM] Connection failed after retries — recreating fresh session from bot token.")
    try:
        await tg_app.stop()
    except Exception:
        pass
    for suffix in (".session", ".session-journal"):
        stale = session_path + suffix
        if os.path.exists(stale):
            try:
                os.replace(stale, stale + ".stale")
                logger.warning(f"[PYROGRAM] Backed up stale {stale} -> {stale}.stale")
            except Exception as e:
                logger.error(f"[PYROGRAM] Failed to back up {stale}: {e}")
    try:
        await tg_app.start()
        me = await tg_app.get_me()
        logger.info(f"[PYROGRAM] Pyrogram reconnected with fresh session! Bot: @{getattr(me, 'username', '?')} (id={getattr(me, 'id', '?')})")
        await _register_main_client()
        await _start_extra_clients()
        await restore_cached_strm_files()
        await register_telegram_webhook()
    except Exception as e:
        logger.error(f"[PYROGRAM] Fresh-session start failed: {e}")

async def stop_pyrogram():
    try:
        if tg_app and tg_app.is_connected:
            await apex_pool.unregister(1)
            await tg_app.stop()
    except Exception as e:
        logger.warning(f"[PYROGRAM] shutdown primary: {e}")
    for idx, client in extra_clients:
        try:
            await apex_pool.unregister(idx)
            if client.is_connected:
                await client.stop()
        except Exception as e:
            logger.warning(f"[PYROGRAM] shutdown extra {idx}: {e}")

async def start_background_tasks(app):
    async def _guard():
        try:
            await start_pyrogram()
        except Exception as e:
            logger.exception(f"[PYROGRAM] start_pyrogram crashed: {e}")
    app['pyrogram_task'] = asyncio.create_task(_guard())

async def cleanup_background_tasks(app):
    await stop_pyrogram()

app = web.Application()
app.add_routes(routes)
app.on_startup.append(start_background_tasks)
app.on_cleanup.append(cleanup_background_tasks)

if __name__ == "__main__":
    logger.info(f"Starting TG-Drive High-Speed 5G Stream Proxy on {HOST}:{PORT}")
    web.run_app(app, host=HOST, port=PORT)
