import os
import sys
import re
import json
import glob
import logging
import asyncio
import socket
import urllib.parse
import time
import aiohttp
import httpx
from aiohttp import web

# Pyrogram imports for MTProto direct chunk streaming
from pyrogram import Client, filters
from pyrogram.enums import ChatType
from pyrogram.types import Message
from pyrogram.errors import FloodWait, RPCError, AuthKeyDuplicated

# Telegram's FILE_REFERENCE_EXPIRED (a stored media reference that has expired)
# surfaces in Pyrogram as the pascal-cased `FileReferenceExpired`, but the exact
# top-level export name varies across pyrogram builds. Import defensively and
# fall back to an ID / name / text match on RPCError (see _is_expired_reference).
try:
    from pyrogram.errors import FileReferenceExpired
except ImportError:  # pragma: no cover - depends on installed pyrogram
    FileReferenceExpired = None

# Apex streaming core (telegram-free; wired to Pyrogram below)
from apex_stream import Config, BoundedChunkCache, ClientPool, InFlightRegistry, StreamMetrics, StreamDriver, FileInfo, RangeNotSatisfiable, NoClientAvailable, SourceReferenceExpired
from apex_stream.organize import (
    DEFAULT_LANGUAGE,
    detect_language,
    language_code,
    match_subtitle_media,
    movie_target_dir,
    safe_folder,
    subtitle_is_document,
    subtitle_lang_hint,
    tv_target_dir,
)

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
SUBTITLE_CACHE_FILE = os.path.join(DATA_DIR, "subtitle_ids.json")
# Pending subtitle docs awaiting a video match (and byte mirror to /data)
SUBTITLE_CACHE = {}
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

# ---------------------------------------------------------------------------
# FILE_ID_CACHE is keyed by (chat_id, message_id) as "<chat>:<msg>" so the same
# message id in two different chats can never collide. Legacy plain "<msg>"
# keys (single-channel deployments / pre-upgrade disk caches) remain readable.
# ---------------------------------------------------------------------------

def _cache_key(chat_id, message_id):
    try:
        chat = int(chat_id or 0)
    except (TypeError, ValueError):
        chat = 0
    return f"{chat}:{int(message_id)}"


def _cache_lookup(message_id, chat_id=None):
    """Resolve a FILE_ID_CACHE entry by message id.

    Prefers the composite (chat_id:message_id) key when chat_id is known;
    falls back to legacy plain-message_id keys, and finally to a scan over
    composite keys when only the message id is available (legacy routes). A
    resolved legacy entry whose chat_id contradicts `chat_id` is rejected.
    """
    if chat_id:
        entry = FILE_ID_CACHE.get(_cache_key(chat_id, message_id))
        if isinstance(entry, dict):
            return entry
    legacy = FILE_ID_CACHE.get(str(message_id))
    if isinstance(legacy, dict):
        if chat_id and legacy.get("chat_id") and str(legacy.get("chat_id")) != str(chat_id):
            return None
        return legacy
    if not chat_id:
        for key, entry in FILE_ID_CACHE.items():
            if not isinstance(entry, dict) or not isinstance(key, str) or ":" not in key:
                continue
            try:
                _chat_part, _msg_part = key.rsplit(":", 1)
                if int(_msg_part) == int(message_id):
                    return entry
            except (TypeError, ValueError):
                continue
    return None


def _split_cache_key(key):
    """Return (chat_id, message_id) for a composite key, else (None, msg_id)."""
    if isinstance(key, str) and ":" in key:
        try:
            chat_part, msg_part = key.rsplit(":", 1)
            return int(chat_part), int(msg_part)
        except (TypeError, ValueError):
            return None, key
    return None, key

def _load_subtitle_cache():
    global SUBTITLE_CACHE
    if os.path.exists(SUBTITLE_CACHE_FILE):
        try:
            with open(SUBTITLE_CACHE_FILE, "r") as f:
                SUBTITLE_CACHE = json.load(f)
                logger.info(f"[CACHE] Loaded {len(SUBTITLE_CACHE)} pending subtitles.")
        except Exception as e:
            logger.warning(f"[CACHE] Error loading subtitle_ids.json: {e}")

def _save_subtitle_cache_sync():
    try:
        with open(SUBTITLE_CACHE_FILE, "w") as f:
            json.dump(SUBTITLE_CACHE, f, indent=2)
    except Exception as e:
        logger.warning(f"[CACHE] Error saving subtitle_ids.json: {e}")

async def save_subtitle_cache_async():
    await asyncio.to_thread(_save_subtitle_cache_sync)

async def save_cache_async():
    await asyncio.to_thread(_save_cache_sync)

def save_cache():
    _save_cache_sync()

load_cache()
_load_subtitle_cache()

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
    entry = _cache_lookup(message_id, chat_id)
    if not isinstance(entry, dict):
        return None
    return FileInfo(
        size=int(entry.get("file_size") or 0),
        name=entry.get("title") or f"Media_{message_id}",
        file_id=entry.get("file_id"),
        mime_type="video/mp4",
    )


async def _refresh_file_reference(client, chat_id, message_id):
    """Re-resolve a fresh media file reference through `client`.

    Telegram file references are short-lived tokens, so on FILE_REFERENCE_EXPIRED
    the source message must be re-fetched via get_messages() and the fresh
    file_id / file_size written back into FILE_ID_CACHE. Each pool client runs
    this through its own connection so a refresh is never shared across bots.
    Returns (file_id, file_size) or None when this client cannot re-resolve it.
    """
    try:
        message = await client.get_messages(chat_id, message_id)
    except Exception as e:
        logger.warning(
            f"[REFRESH] get_messages({chat_id}, {message_id}) failed: {type(e).__name__}: {e}"
        )
        return None
    media = (
        (message.video or message.document or message.audio or message.animation)
        if message else None
    )
    file_id = getattr(media, "file_id", None) if media else None
    if not file_id:
        logger.warning(
            f"[REFRESH] message {chat_id}:{message_id} no longer carries fresh media "
            f"(media={type(media).__name__ if media else None})"
        )
        return None
    return file_id, int(getattr(media, "file_size", 0) or 0)


def _is_expired_reference(exc):
    """True when `exc` is Telegram's FILE_REFERENCE_EXPIRED (any pyrogram layout).

    Pyrogram has historically raised the pascal-cased ``FileReferenceExpired``
    (an ``RPCError`` with ``ID == "FILE_REFERENCE_EXPIRED"``), but the symbol is
    exported at different paths across versions/builds, so match defensively:
    the concrete class when importable, then the RPC error ID, class name, raw
    value, or rendered text via an underscore-insensitive comparison.
    """
    if not isinstance(exc, RPCError):
        return False
    if FileReferenceExpired is not None and isinstance(exc, FileReferenceExpired):
        return True
    probes = (
        getattr(exc, "ID", None),
        type(exc).__name__,
        getattr(exc, "value", None),
        str(exc),
    )
    for probe in probes:
        if not probe:
            continue
        normalized = str(probe).upper().replace("_", "")
        if "FILEREFERENCEEXPIRED" in normalized:
            return True
    return False


async def _fetch_chunks(client, chat_id, message_id, run_start, chunk_count):
    """Yield (offset_in_run, chunk) 1 MiB chunks from one client via MTProto.

    Pyrogram's ``get_file()`` logs-and-swallows RPC/transport failures, so an
    expired Telegram file reference (FILE_REFERENCE_EXPIRED) surfaces as a run
    that ends EARLY instead of an exception. Detect a byte-short run against the
    expected span (derived from the cached file_size), refresh the reference
    through THAT SAME client, and replay the whole run once. Re-delivered chunks
    are deduplicated by the inflight registry against consumer positions, so a
    refresh never re-serves bytes already consumed.
    """
    entry = _cache_lookup(message_id, chat_id)
    if not isinstance(entry, dict):
        return
    file_id = entry.get("file_id")
    if not file_id:
        return

    chunk_bytes = 1024 * 1024
    run_expected = chunk_bytes * chunk_count
    stored_size = int(entry.get("file_size") or 0)
    if stored_size:
        remaining = stored_size - run_start * chunk_bytes
        if remaining < run_expected:
            run_expected = max(remaining, 0)

    for attempt in (1, 2):
        try:
            i = 0
            bytes_yielded = 0
            async for chunk in client.stream_media(file_id, offset=run_start, limit=chunk_count):
                yield (i, chunk)
                i += 1
                bytes_yielded += len(chunk)
            if bytes_yielded >= run_expected:
                return
            logger.warning(
                f"[STREAM] short run {chat_id}:{message_id} ch{run_start}+{chunk_count} "
                f"attempt {attempt}: yielded {bytes_yielded}/{run_expected} bytes "
                f"(client {getattr(client, 'name', '?')})"
            )
        except Exception as exc:
            if not _is_expired_reference(exc):
                raise
            logger.warning(
                f"[STREAM] expired reference {chat_id}:{message_id} ch{run_start}+{chunk_count} "
                f"attempt {attempt} (client {getattr(client, 'name', '?')}): "
                f"{type(exc).__name__}: {exc}"
            )
            if attempt >= 2:
                return
        refreshed = await _refresh_file_reference(client, chat_id, message_id)
        if refreshed is None:
            logger.warning(
                f"[REFRESH] could not refresh {chat_id}:{message_id} "
                f"(client {getattr(client, 'name', '?')})"
            )
            return
        new_file_id, new_size = refreshed
        if new_file_id != file_id:
            entry["file_id"] = new_file_id
        if new_size and int(entry.get("file_size") or 0) != new_size:
            entry["file_size"] = new_size
        if new_size:
            remaining = int(new_size) - run_start * chunk_bytes
            run_expected = min(run_expected, max(remaining, 0))
        await save_cache_async()
        logger.info(
            f"[REFRESH] refreshed expired reference {chat_id}:{message_id} "
            f"(client {getattr(client, 'name', '?')})"
        )
        file_id = new_file_id
        if attempt >= 2:
            return
        # loop back and replay the same run against the fresh reference


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
                max_concurrent_transmissions=APEX_CFG.telegram_max_concurrent,
            )))
        except Exception as e:
            logger.error(f"[PYROGRAM] Error initializing extra client {_ec_idx}: {e}")

def index_media(msg_id, chat_id, file_id, file_size, file_name):
    """Core ingestion: cache the file and create a .strm for Jellyfin."""
    if not file_id or not msg_id:
        return False

    cached = _cache_lookup(msg_id, chat_id)
    if isinstance(cached, dict) and cached.get("file_id") == file_id:
        return cached.get("title") or True

    is_tv, title, show_name, season, episode = parse_media_type(file_name)
    language = detect_language(file_name)

    FILE_ID_CACHE[_cache_key(chat_id, msg_id)] = {
        "file_id": file_id,
        "chat_id": chat_id,
        "file_size": file_size,
        "title": title,
        "is_tv": is_tv,
        "show_name": show_name,
        "season": season,
        "episode": episode,
        "language": language
    }
    save_cache()

    strm_name = create_strm_file(msg_id, file_id, title, is_tv, show_name, season, episode, chat_id, language)
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

        file_name = media_file_name(message, media)
        if subtitle_is_document(file_name, getattr(media, "mime_type", None)):
            placed = await _place_subtitle(message.id, chat_id, media.file_id, file_name)
            logger.info(f"[PYROGRAM] 🎞️ Subtitle handled: {file_name} placed={placed}")
            return

        strm_name = index_media(message.id, chat_id, media.file_id, media.file_size or 0, file_name)
        if strm_name:
            logger.info(f"[PYROGRAM] 🎉 Ingested '{strm_name}' from chat {chat_id}")
            try:
                await _retry_pending_subtitles()
            except Exception as e:
                logger.warning(f"[PYROGRAM] subtitle retry failed: {e}")
            await request_jellyfin_scan()
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
            in_memory=True,
            max_concurrent_transmissions=APEX_CFG.telegram_max_concurrent,
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
        "pending_subtitles": sum(1 for s in SUBTITLE_CACHE.values() if isinstance(s, dict) and not s.get("placed")),
        "placed_subtitles": sum(1 for s in SUBTITLE_CACHE.values() if isinstance(s, dict) and s.get("placed")),
        "movies_dir": MOVIES_DIR,
        "shows_dir": SHOWS_DIR,
        "organization": "language-based (Movies/<Language>, TV Shows/<Language>/<Show>/Season NN)",
        "stream_concurrency_per_client": APEX_CFG.telegram_max_concurrent,
        "cache_ttl_seconds": APEX_CFG.cache_ttl_seconds,
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

async def _download_subtitle_bytes(file_id):
    """Fetch a small subtitle document's bytes via the primary client."""
    if not tg_app or not tg_app.is_connected or not file_id:
        return None
    try:
        data = await tg_app.download_media(file_id, in_memory=True)
        if isinstance(data, (bytes, bytearray)):
            return bytes(data)
        buff = getattr(data, "getbuffer", None)
        if buff is not None:
            return bytes(buff)
        raw = bytes(data)
        return raw
    except Exception as e:
        logger.warning(f"[SUBTITLE] download_media failed: {type(e).__name__}: {e}")
        return None


def _subtitle_target_path(entry, subtitle_ext, subtitle_file_name):
    """Resolve the sidecar path for a subtitle next to the matched video .strm.

    Jellyfin resolves external subtitles by base-name + language tag, e.g.
    `Movie.eng.srt` beside `Movie.strm`. Returns None if the entry is unusable.
    """
    if not isinstance(entry, dict) or not entry.get("title"):
        return None
    lang_name = subtitle_lang_hint(subtitle_file_name)
    code = language_code(lang_name) or "und"
    base = safe_folder(entry.get("title"))
    if entry.get("is_tv") and entry.get("show_name"):
        target_dir = tv_target_dir(
            SHOWS_DIR,
            entry.get("language") or detect_language(entry.get("title") or ""),
            entry.get("show_name"),
            entry.get("season"),
        )
    else:
        target_dir = movie_target_dir(
            MOVIES_DIR,
            entry.get("language") or detect_language(entry.get("title") or ""),
        )
    return os.path.join(target_dir, f"{base}.{code}{subtitle_ext}")


async def _place_subtitle(msg_id, chat_id, file_id, file_name):
    """Match a subtitle document to an indexed video and write the sidecar file.

    Stores metadata in SUBTITLE_CACHE for persistence across redeploys; a
    subtitle without a video match yet is retried on every following ingest and
    on cache restore.
    """
    subtitle_ext = os.path.splitext(file_name or "")[1].lower()
    if not subtitle_ext:
        subtitle_ext = ".srt"

    SUBTITLE_CACHE[str(msg_id)] = {
        "file_id": file_id,
        "chat_id": chat_id,
        "file_name": file_name,
        "placed": False,
        "target": None,
    }
    await save_subtitle_cache_async()

    match = match_subtitle_media(file_name, FILE_ID_CACHE)
    if not match:
        return False

    video_msg, video_entry, _score = match
    target = _subtitle_target_path(video_entry, subtitle_ext, file_name)
    if not target:
        return False

    data = await _download_subtitle_bytes(file_id)
    if not data:
        return False

    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as f:
            f.write(data)
    except Exception as e:
        logger.warning(f"[SUBTITLE] write failed for {target}: {e}")
        return False

    SUBTITLE_CACHE[str(msg_id)] = {
        "file_id": file_id,
        "chat_id": chat_id,
        "file_name": file_name,
        "placed": True,
        "target": target,
        "video_msg": video_msg,
    }
    await save_subtitle_cache_async()
    logger.info(f"[SUBTITLE] ✅ Placed subtitle for {video_msg}: {target}")
    return True


async def _retry_pending_subtitles():
    """Attempt to place any queued subtitles that now have a video match."""
    placed = 0
    for msg_id, sub in list(SUBTITLE_CACHE.items()):
        if not isinstance(sub, dict) or sub.get("placed"):
            continue
        file_name = sub.get("file_name")
        match = match_subtitle_media(file_name or "", FILE_ID_CACHE)
        if not match:
            continue
        video_msg, video_entry, _score = match
        ext = os.path.splitext(file_name or "")[1].lower() or ".srt"
        target = _subtitle_target_path(video_entry, ext, file_name or "")
        if not target or os.path.exists(target):
            continue
        data = await _download_subtitle_bytes(sub.get("file_id"))
        if not data:
            continue
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as f:
                f.write(data)
        except Exception as e:
            logger.warning(f"[SUBTITLE] retry write failed {target}: {e}")
            continue
        SUBTITLE_CACHE[str(msg_id)] = {**sub, "placed": True, "target": target, "video_msg": video_msg}
        placed += 1
    if placed:
        await save_subtitle_cache_async()
        logger.info(f"[SUBTITLE] Placed {placed} previously-queued subtitle(s).")
    return placed


def _create_strm_file_sync(msg_id, file_id, clean_title, is_tv=False, show_name=None, season=None, episode=None, chat_id=None, language=None):
    # Base name normalized via safe_folder so Jellyfin can match external
    # subtitle sidecars (base.<lang>.ext) against the .strm byte-for-byte.
    strm_base = safe_folder(clean_title)
    if is_tv and show_name:
        target_dir = tv_target_dir(SHOWS_DIR, language or DEFAULT_LANGUAGE, show_name, season)
        os.makedirs(target_dir, exist_ok=True)
        strm_filename = f"{strm_base}.strm"
    else:
        target_dir = movie_target_dir(MOVIES_DIR, language or DEFAULT_LANGUAGE)
        os.makedirs(target_dir, exist_ok=True)
        strm_filename = f"{strm_base}.strm"

    strm_path = os.path.join(target_dir, strm_filename)
    if chat_id:
        stream_url = f"http://127.0.0.1:8080/stream/{chat_id}/{msg_id}/video.mp4"
    else:
        stream_url = f"http://127.0.0.1:8080/stream_file?file_id={file_id}&message_id={msg_id}&filename={clean_title}.mp4"

    with open(strm_path, "w") as f:
        f.write(stream_url)

    logger.info(f"[AUTO-SYNC] 🎉 Created .strm file: {strm_filename} -> {strm_path}")
    return strm_filename, target_dir

async def create_strm_file_async(msg_id, file_id, clean_title, is_tv=False, show_name=None, season=None, episode=None, chat_id=None, language=None):
    strm_filename, target_dir = await asyncio.to_thread(_create_strm_file_sync, msg_id, file_id, clean_title, is_tv, show_name, season, episode, chat_id, language)
    asyncio.create_task(fetch_tmdb_poster(show_name if is_tv else clean_title, target_dir, clean_title))
    return strm_filename

def create_strm_file(msg_id, file_id, clean_title, is_tv=False, show_name=None, season=None, episode=None, chat_id=None, language=None):
    strm_filename, target_dir = _create_strm_file_sync(msg_id, file_id, clean_title, is_tv, show_name, season, episode, chat_id, language)
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

# Debounced refresh: bursts of webhook ingests must coalesce into ONE library
# scan instead of hammering Jellyfin's /Library/Refresh per message.
_JF_SCAN_LAST = 0.0
_JF_SCAN_MIN_INTERVAL = int(get_env("APEX_JELLYFIN_SCAN_INTERVAL", default="15"))
_JF_SCAN_LOCK = asyncio.Lock()

async def request_jellyfin_scan():
    """Coalesce ingest-triggered library refreshes into one scan per interval."""
    global _JF_SCAN_LAST
    async with _JF_SCAN_LOCK:
        now = time.monotonic()
        if now - _JF_SCAN_LAST < _JF_SCAN_MIN_INTERVAL:
            return
        _JF_SCAN_LAST = now
        await trigger_jellyfin_scan()

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
                await asyncio.sleep(2)
    logger.debug(f"[TELEGRAM] {method} failed after 3 attempts: {type(last_error).__name__}: {last_error}")
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
        if ok:
            logger.info(f"[WEBHOOK] setWebhook registered: {(result or {}).get('description', 'ok')}")
            return True, webhook_url, (result or {}).get("description", "ok")
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

        # Subtitle documents (srt/ass/vtt) are placed beside the matching video
        # .strm instead of being indexed as playable media themselves.
        if subtitle_is_document(file_name, media_obj.get("mime_type")):
            placed = await _place_subtitle(msg_id, chat_id, file_id, file_name)
            logger.info(f"[WEBHOOK] subtitle document handled: {file_name} placed={placed}")
            return web.json_response({"ok": True})

        is_tv, title, show_name, season, episode = parse_media_type(file_name)
        language = detect_language(file_name)

        if file_id and msg_id:
            if _cache_lookup(msg_id, chat_id):
                return web.json_response({"ok": True, "dedup": True})

            FILE_ID_CACHE[_cache_key(chat_id, msg_id)] = {
                "file_id": file_id,
                "chat_id": chat_id,
                "file_size": file_size,
                "title": title,
                "is_tv": is_tv,
                "show_name": show_name,
                "season": season,
                "episode": episode,
                "language": language
            }
            await save_cache_async()

            strm_name = await create_strm_file_async(msg_id, file_id, title, is_tv, show_name, season, episode, chat_id, language)
            logger.info(f"[WEBHOOK] 🎉 Successfully indexed media from Webhook: {strm_name}")
            await request_jellyfin_scan()
            try:
                await _retry_pending_subtitles()
            except Exception as e:
                logger.warning(f"[WEBHOOK] subtitle retry failed: {e}")

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


async def _scan_chat_history(chat_id, limit, indexed_list):
    """Scan one chat's history and re-ingest every media message into FILE_ID_CACHE + .strm."""
    indexed = 0
    skipped = 0
    async for message in tg_app.get_chat_history(chat_id, limit=limit):
        media = message.video or message.document or message.audio or message.animation
        if not media or not getattr(media, "file_id", None):
            continue
        chat = message.chat
        msg_chat_id = chat.id if chat else chat_id
        if msg_chat_id is None or not allowed_chat(chat):
            continue
        msg_id = message.id
        existing = _cache_lookup(msg_id, msg_chat_id)
        if isinstance(existing, dict) and existing.get("file_id") == media.file_id:
            skipped += 1
            continue
        file_name = media_file_name(message, media)
        if subtitle_is_document(file_name, getattr(media, "mime_type", None)):
            placed = await _place_subtitle(msg_id, msg_chat_id, media.file_id, file_name)
            logger.info(f"[REINDEX] Subtitle handled: {file_name} placed={placed}")
            skipped += 1
            continue
        is_tv, title, show_name, season, episode = parse_media_type(file_name)
        language = detect_language(file_name)
        FILE_ID_CACHE[_cache_key(msg_chat_id, msg_id)] = {
            "file_id": media.file_id,
            "chat_id": msg_chat_id,
            "file_size": media.file_size or 0,
            "title": title,
            "is_tv": is_tv,
            "show_name": show_name,
            "season": season,
            "episode": episode,
            "language": language
        }
        strm_name = await create_strm_file_async(msg_id, media.file_id, title, is_tv, show_name, season, episode, msg_chat_id, language)
        logger.info(f"[REINDEX] Re-ingested '{strm_name}' (msg {msg_id}, chat {msg_chat_id})")
        indexed += 1
    indexed_list.append({"chat_id": chat_id, "indexed": indexed, "skipped": skipped})


async def _resolve_reindex_targets(explicit_chat_id):
    """Return a de-duplicated list of chat ids to scan for media."""
    targets = set()
    if explicit_chat_id:
        targets.add(int(explicit_chat_id))
        return list(targets)

    for entry in FILE_ID_CACHE.values():
        if isinstance(entry, dict) and entry.get("chat_id"):
            targets.add(int(entry["chat_id"]))

    for raw in (RAW_CHANNEL_ID or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            if raw.lstrip("-").isdigit() or raw.lstrip("@").isdigit():
                chat = await tg_app.get_chat(int(raw))
            else:
                chat = await tg_app.get_chat(raw)
            targets.add(chat.id)
        except Exception as e:
            logger.warning(f"[REINDEX] Could not resolve configured chat '{raw}': {e}")

    if DETECTED_CHANNEL_ID:
        targets.add(int(DETECTED_CHANNEL_ID))

    if tg_app:
        try:
            async for dialog in tg_app.get_dialogs(limit=200):
                dchat = dialog.chat
                if dchat is None or dchat.id in targets:
                    continue
                ctype = getattr(dchat, "type", None)
                if ctype in (ChatType.PRIVATE, ChatType.CHANNEL, ChatType.SUPERGROUP, ChatType.GROUP):
                    targets.add(dchat.id)
        except Exception as e:
            logger.warning(f"[REINDEX] get_dialogs failed: {e}")
    return list(targets)


@routes.post("/reindex")
@routes.get("/reindex")
async def reindex_route(request):
    """Re-scan the bot's channels + DMs and re-ingest every media message.

    Rebuilds FILE_ID_CACHE and .strm files from Telegram chat history — useful after a
    fresh deploy where file_ids.json was empty (ingestion only receives NEW updates).
    """
    if not tg_app or not tg_app.is_connected:
        return web.json_response({"ok": False, "error": "Pyrogram client not connected"}, status=503)
    try:
        limit = int(request.query.get("limit", "200"))
    except (TypeError, ValueError):
        limit = 200
    explicit = request.query.get("chat_id")

    try:
        targets = await _resolve_reindex_targets(explicit)
    except Exception as e:
        return web.json_response({"ok": False, "error": f"resolve targets: {e}"}, status=500)

    if not targets:
        return web.json_response({"ok": False, "error": "no target chats resolved (configure TELEGRAM_ALLOWED_CHAT_IDS or visit the configured channel/dialog)"}, status=404)

    results = []
    failures = []
    for chat_id in targets:
        try:
            await _scan_chat_history(chat_id, limit, results)
        except FloodWait as e:
            logger.info(f"[REINDEX] FloodWait on chat {chat_id}: sleeping {e.value}s")
            await asyncio.sleep(e.value)
            try:
                await _scan_chat_history(chat_id, limit, results)
            except Exception as e2:
                logger.warning(f"[REINDEX] chat {chat_id} retry failed: {e2}")
                failures.append({"chat_id": chat_id, "error": f"{type(e2).__name__}: {e2}"})
        except Exception as e:
            logger.warning(f"[REINDEX] chat {chat_id} failed: {type(e).__name__}: {e}")
            failures.append({"chat_id": chat_id, "error": f"{type(e).__name__}: {e}"})

    await save_cache_async()
    await restore_cached_strm_files()
    await trigger_jellyfin_scan()
    total = sum(r["indexed"] for r in results)
    return web.json_response({"ok": True, "targets": targets, "chats": results, "failures": failures, "total_indexed": total, "cache_now": len(FILE_ID_CACHE)})


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
        entry = _cache_lookup(message_id)
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

    entry = _cache_lookup(message_id, chat_id)
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

    if request.method == "HEAD":
        # Jellyfin / ffprobe probes hit HEAD to learn the size before starting a
        # play; don't burn Telegram bandwidth generating a body that aiohttp
        # would drop anyway.
        return response

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
    snap["cache"] = apex_cache.stats()
    languages = {}
    for data in FILE_ID_CACHE.values():
        if isinstance(data, dict):
            lang = data.get("language") or "unknown"
            languages[lang] = languages.get(lang, 0) + 1
    snap["languages"] = languages
    snap["pending_subtitles"] = sum(1 for s in SUBTITLE_CACHE.values() if isinstance(s, dict) and not s.get("placed"))
    snap["placed_subtitles"] = sum(1 for s in SUBTITLE_CACHE.values() if isinstance(s, dict) and s.get("placed"))
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
    for key, data in FILE_ID_CACHE.items():
        if isinstance(data, dict):
            _chat_from_key, msg_id = _split_cache_key(key)
            file_id = data.get("file_id")
            title = data.get("title") or f"Media_{msg_id}"
            is_tv = data.get("is_tv", False)
            show_name = data.get("show_name")
            season = data.get("season")
            episode = data.get("episode")
            language = data.get("language") or detect_language(title) or DEFAULT_LANGUAGE

            if file_id and title:
                chat_id = data.get("chat_id") or _chat_from_key
                await create_strm_file_async(msg_id, file_id, title, is_tv, show_name, season, episode, chat_id, language)
                count += 1
    if count > 0:
        logger.info(f"[RESTORE] Restored {count} .strm file(s) from persistent disk cache.")
        await trigger_jellyfin_scan()

    # Queued subtitles may now resolve against the restored media index.
    if SUBTITLE_CACHE and tg_app and tg_app.is_connected:
        try:
            await _retry_pending_subtitles()
        except Exception as e:
            logger.warning(f"[RESTORE] subtitle retry failed: {e}")
    if SUBTITLE_CACHE:
        still = sum(1 for s in SUBTITLE_CACHE.values() if isinstance(s, dict) and not s.get("placed"))
        if still:
            logger.info(f"[RESTORE] {still} subtitle(s) still awaiting a video match.")

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
