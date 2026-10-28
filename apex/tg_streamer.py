import os
import sys
import re
import json
import glob
import logging
import asyncio
import socket
import struct
import urllib.parse
import time
import hmac
import hashlib
import uuid
import aiohttp
from aiohttp import web
import httpx
import shutil
from typing import Any, Dict, List, Optional, Set, Tuple

_BLACKLIST_PATTERNS = [
    re.compile(r"mot\s*test\s*servicing", re.IGNORECASE),
    re.compile(r"(?:ms\.?\s*rose|mr\.?\s*cane|rose\s*&?\s*cane)", re.IGNORECASE),
    re.compile(r"wwe\s*money\s*in\s*the\s*bank", re.IGNORECASE),
    re.compile(r"\bwwe\b", re.IGNORECASE),
]


def is_blacklisted(text: Optional[str]) -> bool:
    """Return True if text matches unwanted demo / test media to permanently purge."""
    if not text:
        return False
    return any(bool(p.search(str(text))) for p in _BLACKLIST_PATTERNS)

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
from apex_stream import Config, BoundedChunkCache, ClientPool, InFlightRegistry, StreamMetrics, StreamDriver, FileInfo, RangeNotSatisfiable, NoClientAvailable, SourceReferenceExpired, MediaCatalog
from apex_stream.organize import (
    DEFAULT_LANGUAGE,
    detect_language,
    language_code,
    language_name_from_code,
    match_subtitle_media,
    strm_base,
    strm_dir,
    subtitle_is_document,
    subtitle_lang_hint,
    tmdb_language_code,
    tv_show_root,
)
from apex_stream.tmdb_meta import (
    _search_candidates,
    best_tmdb_match,
    build_episode_nfo,
    build_movie_nfo,
    build_tvshow_nfo,
    extract_imdb_id,
    nfo_needs_repair,
    parse_media_meta,
)
from apex_stream.subtitles import (
    inject_subtitles_for_entry,
    is_valid_subtitle_file,
    purge_corrupted_subtitles,
)
from apex_stream.extractor import extract_media_info

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

API_ID = get_env(
    "JELLYFIN_API_ID", "TELEGRAM_API_ID", "TG_API_ID", "API_ID"
)
API_HASH = get_env(
    "JELLYFIN_API_HASH", "TELEGRAM_API_HASH", "TG_API_HASH", "API_HASH"
)
BOT_TOKEN = get_env("TELEGRAM_BOT_TOKEN", "TG_BOT_TOKEN", "BOT_TOKEN")
USER_SESSION_STRING = get_env(
    "USER_SESSION_STRING", "SESSION_STRING", "TG_SESSION_STRING", "STRING_SESSION", default=""
)
RAW_CHANNEL_ID = get_env("TELEGRAM_ALLOWED_CHAT_IDS", "TG_CHANNEL_ID", default="")
TMDB_API_KEY = get_env("TMDB_API_KEY")

# Optional AI title-extraction fallback (omniroute OpenAI-compatible proxy).
# Only used when the rule-based parser + TMDB search both miss, so the fast
# offline path is never slowed down. Both vars must be set or AI is disabled.
OMNIROUTE_URL = get_env("OMNIROUTE_API_URL").rstrip("/") if get_env("OMNIROUTE_API_URL") else ""
OMNIROUTE_KEY = get_env("OMNIROUTE_API_KEY")
AI_TITLE_MODEL = get_env("AI_TITLE_MODEL", default="auto/best-fast")
# Reasoning models consume tokens on reasoning_content before producing the
# answer; keep the budget large enough that a JSON reply still fits.
AI_TITLE_MAX_TOKENS = int(get_env("AI_TITLE_MAX_TOKENS", default="1024"))
_AI_TITLE_CACHE = {}
_AI_DISAMBIGUATE_CACHE = {}


# Public URL Telegram must deliver bot updates to (through the Apex gateway).
PUBLIC_BASE_URL = get_env(
    "PUBLIC_BASE_URL",
    "PUBLIC_URL",
    default="https://jishnupg-filetolink.hf.space",
)
WEBHOOK_PATH = "/tg-stream/telegram-webhook"

DATA_DIR = os.environ.get("APEX_DATA_DIR", "/data/jellyfin")
if not USER_SESSION_STRING:
    _session_file = os.path.join(DATA_DIR, "user_session.string")
    if os.path.exists(_session_file):
        try:
            with open(_session_file, "r", encoding="utf-8") as _sf:
                USER_SESSION_STRING = _sf.read().strip()
                if USER_SESSION_STRING:
                    logger.info("[PYROGRAM] Loaded USER_SESSION_STRING from persisted storage.")
        except Exception as _e:
            logger.warning(f"[PYROGRAM] Could not read user_session.string: {_e}")
MOVIES_DIR = os.path.join(DATA_DIR, "media/Movies")
SHOWS_DIR = os.path.join(DATA_DIR, "media/TV Shows")
CACHE_FILE = os.path.join(DATA_DIR, "file_ids.json")
CONFIG_FILE = os.path.join(DATA_DIR, "channel_config.json")
CATALOG_FILE = os.path.join(DATA_DIR, "media_catalog.sqlite")

try:
    os.makedirs(MOVIES_DIR, exist_ok=True)
    os.makedirs(SHOWS_DIR, exist_ok=True)
except Exception as _e:
    logger.warning(f"[INIT] Could not create media directories: {_e}")
MEDIA_CATALOG = MediaCatalog(CATALOG_FILE)

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

    # Normalize any inverted msg_id / chat_id in cached entries (e.g. from prior swapped ingestion)
    normalized = 0
    for key, data in list(FILE_ID_CACHE.items()):
        if isinstance(data, dict):
            m = data.get("msg_id")
            c = data.get("chat_id")
            if isinstance(m, int) and isinstance(c, int) and m < 0 and c > 0:
                data["msg_id"] = c
                data["chat_id"] = m
                correct_key = _cache_key(m, c)
                FILE_ID_CACHE[correct_key] = data
                normalized += 1
    if normalized:
        logger.info(f"[CACHE] Normalized {normalized} inverted msg_id/chat_id cache entries.")

    # Clean release-size noise and heal misidentified metadata in cache
    cache_updated = False
    size_pattern = re.compile(r"(?i)\b\d+(?:\.\d+)?\s*(?:mb|gb|kib|mib|gib|tb)\b")
    for key, data in list(FILE_ID_CACHE.items()):
        if not isinstance(data, dict):
            continue
        raw_name = data.get("file_name") or data.get("raw_name") or data.get("title") or ""
        raw_lower = raw_name.lower()
        tmdb_t = (data.get("tmdb_title") or "").lower()
        title_l = (data.get("title") or "").lower()



        # 0. Heal bogus/synthetic TMDB IDs (e.g. raw IMDb numbers like 5568976, 3215778)
        if data.get("tmdb_id") in (5568976, 3215778):
            data.pop("tmdb_id", None)
            cache_updated = True

        # 0b. Heal Spider-Man: Homecoming (2017)
        if ("homecoming" in raw_lower or "spiderman homecoming" in title_l) and ("spider" in raw_lower or "spiderman" in raw_lower or "wmr" in raw_lower):
            data["title"] = "Spider-Man: Homecoming"
            data["clean_title"] = "Spider-Man: Homecoming"
            data["search_title"] = "spider-man homecoming"
            data["tmdb_id"] = 315635
            data["imdb_id"] = "tt2250912"
            data["year"] = 2017
            data["tmdb_year"] = 2017
            data["strm_base"] = "Spider-Man: Homecoming (2017)"
            lang = data.get("language") or "English"
            data["target_dir"] = os.path.join(MOVIES_DIR, lang, "Spider-Man: Homecoming (2017)")
            data["strm_path"] = os.path.join(data["target_dir"], "Spider-Man: Homecoming (2017).strm")
            data["_force_art_refresh"] = True
            cache_updated = True
            logger.info(f"[CACHE-HEAL] Corrected Spider-Man Homecoming metadata for '{raw_name}'")

        # 1. Heal Generation Iron misidentification on Iron Man movies
        is_gen_iron = (
            data.get("tmdb_id") == 210879
            or "generation iron" in (data.get("tmdb_title") or "").lower()
            or "generation iron" in (data.get("title") or "").lower()
            or "generation iron" in (data.get("strm_base") or "").lower()
        )
        if is_gen_iron or ("iron" in raw_lower and "man" in raw_lower and ("3" in raw_lower or data.get("year") == 2013)):
            data["title"] = "Iron Man 3"
            data["clean_title"] = "Iron Man 3"
            data["search_title"] = "iron man 3"
            data["year"] = 2013
            data["tmdb_id"] = 68721
            data["tmdb_title"] = "Iron Man 3"
            data["tmdb_year"] = 2013
            data["imdb_id"] = "tt1300854"
            data["strm_base"] = "Iron Man 3 (2013)"
            lang = data.get("language") or "English"
            data["target_dir"] = os.path.join(MOVIES_DIR, lang, "Iron Man 3 (2013)")
            data["strm_path"] = os.path.join(data["target_dir"], "Iron Man 3 (2013).strm")
            data["_force_art_refresh"] = True
            cache_updated = True
            logger.info(f"[CACHE-HEAL] Corrected Generation Iron to real Iron Man 3 for '{raw_name}'")

        # 2. Heal Khalifa misidentification and assign authentic metadata & artwork
        is_khalifa = (
            "khalifa" in raw_lower
            or "khalifa" in (data.get("tmdb_title") or "").lower()
            or "khalifa" in (data.get("title") or "").lower()
            or "khalifa" in (data.get("strm_base") or "").lower()
        )
        if is_khalifa:
            data["title"] = "Khalifa"
            data["clean_title"] = "Khalifa"
            data["search_title"] = "khalifa"
            data["tmdb_title"] = "Khalifa"
            data["tmdb_id"] = 1036081
            data["imdb_id"] = "tt22797990"
            data["year"] = 2026
            data["tmdb_year"] = 2026
            data["strm_base"] = "Khalifa (2026)"
            lang = data.get("language") or "Malayalam"
            data["target_dir"] = os.path.join(MOVIES_DIR, lang, "Khalifa (2026)")
            data["strm_path"] = os.path.join(data["target_dir"], "Khalifa (2026).strm")
            data["poster_url"] = "https://m.media-amazon.com/images/M/MV5BMWM2ZDY1MjQtN2RkZS00MWVmLWEzOWItZDIyZGNkNTQ3OWIwXkEyXkFqcGc@._V1_.jpg"
            data["backdrop_url"] = "https://images.metahub.space/background/medium/tt22797990/img"
            data["_force_art_refresh"] = True
            cache_updated = True
            logger.info(f"[CACHE-HEAL] Corrected Khalifa metadata and artwork for '{raw_name}'")

        # 2b. Heal The Paradise (2026) misidentification
        is_paradise = (
            "paradise" in raw_lower
            and "siege" not in raw_lower
            and ("2026" in raw_lower or data.get("year") == 2026 or "the paradise" in (data.get("title") or "").lower())
        )
        if is_paradise:
            data["title"] = "The Paradise"
            data["clean_title"] = "The Paradise"
            data["search_title"] = "the paradise"
            data["imdb_id"] = "tt31969655"
            data["year"] = 2026
            data["strm_base"] = "The Paradise (2026)"
            lang = data.get("language") or "Telugu"
            data["target_dir"] = os.path.join(MOVIES_DIR, lang, "The Paradise (2026)")
            data["strm_path"] = os.path.join(data["target_dir"], "The Paradise (2026).strm")
            data["poster_url"] = "https://m.media-amazon.com/images/M/MV5BMGI3N2I1ZWUtYzMyZS00MTRlLThlNDUtNWZhY2MxZTlmMDJjXkEyXkFqcGc@._V1_.jpg"
            data["_force_art_refresh"] = True
            cache_updated = True
            logger.info(f"[CACHE-HEAL] Corrected The Paradise metadata and artwork for '{raw_name}'")

        # 2c. Heal Ne Zha 1 and 2 metadata
        if "nezha" in raw_lower or ("ne" in raw_lower and "zha" in raw_lower):
            is_sequel = "2" in raw_lower or "2025" in raw_lower or data.get("year") == 2025
            if is_sequel:
                data["title"] = "Ne Zha 2"
                data["clean_title"] = "Ne Zha 2"
                data["search_title"] = "ne zha 2"
                data["imdb_id"] = "tt34956443"
                data["year"] = 2025
                data["strm_base"] = "Ne Zha 2 (2025)"
                lang = data.get("language") or "Chinese"
                data["target_dir"] = os.path.join(MOVIES_DIR, lang, "Ne Zha 2 (2025)")
                data["strm_path"] = os.path.join(data["target_dir"], "Ne Zha 2 (2025).strm")
                data["poster_url"] = "https://m.media-amazon.com/images/M/MV5BNmU2N2RkNDYtMzM2My00OWRlLWE4YmMtOGQxM2EyNjgzZTk2XkEyXkFqcGc@._V1_.jpg"
                data["_force_art_refresh"] = True
                cache_updated = True
                logger.info(f"[CACHE-HEAL] Corrected Ne Zha 2 metadata and artwork for '{raw_name}'")
            else:
                data["title"] = "Ne Zha"
                data["clean_title"] = "Ne Zha"
                data["search_title"] = "ne zha"
                data["imdb_id"] = "tt9262630"
                data["year"] = 2019
                data["strm_base"] = "Ne Zha (2019)"
                lang = data.get("language") or "Chinese"
                data["target_dir"] = os.path.join(MOVIES_DIR, lang, "Ne Zha (2019)")
                data["strm_path"] = os.path.join(data["target_dir"], "Ne Zha (2019).strm")
                data["poster_url"] = "https://m.media-amazon.com/images/M/MV5BNjgzNTQ1YjEtNDFmMS00MDhmLWE3YTctN2I4OGY5MDFjMTg4XkEyXkFqcGc@._V1_.jpg"
                data["_force_art_refresh"] = True
                cache_updated = True
                logger.info(f"[CACHE-HEAL] Corrected Ne Zha (2019) metadata and artwork for '{raw_name}'")

        # 2d. Heal It: Welcome to Derry metadata and artwork
        is_derry = "derry" in raw_lower or "welcome to derry" in (data.get("title") or "").lower() or "it: welcome to derry" in (data.get("title") or "").lower()
        if is_derry:
            data["is_tv"] = True
            data["show_name"] = "It: Welcome to Derry"
            data["title"] = "It: Welcome to Derry"
            data["clean_title"] = "It: Welcome to Derry"
            data["search_title"] = "it welcome to derry"
            data["imdb_id"] = "tt19244304"
            data["tmdb_id"] = 200875
            data["year"] = 2025
            lang = data.get("language") or "English"
            data["language"] = lang
            s_match = re.search(r"(?i)[sS](\d{1,2})[eE](\d{1,2})", raw_name)
            if s_match:
                s_num = int(s_match.group(1))
                e_num = int(s_match.group(2))
            else:
                s_num = int(data.get("season") or 1)
                e_num = int(data.get("episode") or 1)
            data["season"] = s_num
            data["episode"] = e_num
            show_root = tv_show_root(SHOWS_DIR, lang, "It: Welcome to Derry", 2025)
            data["show_root"] = show_root
            data["target_dir"] = os.path.join(show_root, f"Season {s_num:02d}")
            data["strm_base"] = f"It: Welcome to Derry - S{s_num:02d}E{e_num:02d}"
            data["strm_path"] = os.path.join(data["target_dir"], f"{data['strm_base']}.strm")
            data["poster_url"] = "https://m.media-amazon.com/images/M/MV5BZWE0ZWM1MjUtNmYzYS00NzU3LTkxYmQtNmI3NTc0ZTY1NmVlXkEyXkFqcGc@._V1_.jpg"
            data["backdrop_url"] = "https://images.metahub.space/background/medium/tt19244304/img"
            data["_force_art_refresh"] = True
            cache_updated = True
            logger.info(f"[CACHE-HEAL] Corrected It: Welcome to Derry metadata and artwork for '{raw_name}' -> S{s_num:02d}E{e_num:02d}")

        # 2e. Heal Wednesday metadata (e.g. S02E07 previously misparsed as The Money)
        is_wednesday = "wednesday" in raw_lower or "wednesday" in (data.get("title") or "").lower() or ("money" in (data.get("title") or "").lower() and "wednesday" in raw_name.lower())
        if is_wednesday:
            data["is_tv"] = True
            data["show_name"] = "Wednesday"
            data["title"] = "Wednesday"
            data["clean_title"] = "Wednesday"
            data["search_title"] = "wednesday"
            data["imdb_id"] = "tt13443470"
            data["year"] = 2022
            lang = data.get("language") or "English"
            data["language"] = lang
            s_match = re.search(r"(?i)[sS](\d{1,2})[eE](\d{1,2})", raw_name)
            if s_match:
                s_num = int(s_match.group(1))
                e_num = int(s_match.group(2))
            else:
                s_num = int(data.get("season") or 2)
                e_num = int(data.get("episode") or 7)
            data["season"] = s_num
            data["episode"] = e_num
            show_root = tv_show_root(SHOWS_DIR, lang, "Wednesday", 2022)
            data["show_root"] = show_root
            data["target_dir"] = os.path.join(show_root, f"Season {s_num:02d}")
            data["strm_base"] = f"Wednesday - S{s_num:02d}E{e_num:02d}"
            data["strm_path"] = os.path.join(data["target_dir"], f"{data['strm_base']}.strm")
            data["poster_url"] = "https://m.media-amazon.com/images/M/MV5BMDE1NjNmNmQtMGQ5OC00NzczLTgwMDItNTI3NDZjNDk2NDYyXkEyXkFqcGc@._V1_.jpg"
            data["backdrop_url"] = "https://images.metahub.space/background/medium/tt13443470/img"
            data["_force_art_refresh"] = True
            cache_updated = True
            logger.info(f"[CACHE-HEAL] Corrected Wednesday metadata and artwork for '{raw_name}' -> S{s_num:02d}E{e_num:02d}")

        # 2f. Heal What If...? (tt10168312) if misidentified as 2024 Chinese drama
        is_what_if = "what if" in raw_lower or "what.if" in raw_lower or "what_if" in raw_lower or "what if" in (data.get("title") or "").lower()
        if is_what_if and (data.get("year") == 2024 or data.get("imdb_id") == "tt27122945" or not data.get("imdb_id")):
            data["is_tv"] = True
            data["show_name"] = "What If...?"
            data["title"] = "What If...?"
            data["clean_title"] = "What If...?"
            data["search_title"] = "what if"
            data["imdb_id"] = "tt10168312"
            data["tmdb_id"] = 91363
            data["year"] = 2021
            lang = data.get("language") or "English"
            data["language"] = lang
            s_match = re.search(r"(?i)[sS](\d{1,2})[eE](\d{1,2})", raw_name)
            if s_match:
                s_num = int(s_match.group(1))
                e_num = int(s_match.group(2))
            else:
                s_num = int(data.get("season") or 1)
                e_num = int(data.get("episode") or 1)
            data["season"] = s_num
            data["episode"] = e_num
            show_root = tv_show_root(SHOWS_DIR, lang, "What If...?", 2021)
            data["show_root"] = show_root
            data["target_dir"] = os.path.join(show_root, f"Season {s_num:02d}")
            data["strm_base"] = f"What If...? - S{s_num:02d}E{e_num:02d}"
            data["strm_path"] = os.path.join(data["target_dir"], f"{data['strm_base']}.strm")
            data["poster_url"] = "https://m.media-amazon.com/images/M/MV5BYWFiOTcwNzItMTg5Mi00ZTU4LThkZWYtYWNmZWQ1ODE5ZTVmXkEyXkFqcGc@._V1_.jpg"
            data["backdrop_url"] = "https://images.metahub.space/background/medium/tt10168312/img"
            data["_force_art_refresh"] = True
            cache_updated = True
            logger.info(f"[CACHE-HEAL] Corrected What If...? metadata and artwork for '{raw_name}' -> S{s_num:02d}E{e_num:02d}")

        # 2g. Heal Derry if accidentally misidentified in cache
        if "derry" in raw_lower and ("summer i turned pretty" in raw_lower or "the summer i turned pretty" in title_l or data.get("tmdb_id") == 194766):
            s_match = re.search(r"(?i)[sS](\d{1,2})[eE](\d{1,2})", raw_name)
            s_num = int(s_match.group(1)) if s_match else int(data.get("season") or 1)
            e_num = int(s_match.group(2)) if s_match else int(data.get("episode") or 1)
            data["title"] = "It: Welcome to Derry"
            data["clean_title"] = "It: Welcome to Derry"
            data["show_name"] = "It: Welcome to Derry"
            data["is_tv"] = True
            data["year"] = 2025
            data["season"] = s_num
            data["episode"] = e_num
            data["imdb_id"] = "tt19244304"
            data["tmdb_id"] = 200875
            data["poster_url"] = "https://m.media-amazon.com/images/M/MV5BZWE0ZWM1MjUtNmYzYS00NzU3LTkxYmQtNmI3NTc0ZTY1NmVlXkEyXkFqcGc@._V1_.jpg"
            data["backdrop_url"] = "https://images.metahub.space/background/medium/tt19244304/img"
            show_root = tv_show_root(SHOWS_DIR, data.get("language") or "English", "It: Welcome to Derry", 2025)
            data["show_root"] = show_root
            data["target_dir"] = os.path.join(show_root, f"Season {s_num:02d}")
            data["strm_base"] = f"It: Welcome to Derry - S{s_num:02d}E{e_num:02d}"
            old_strm = data.get("strm_path")
            new_strm = os.path.join(data["target_dir"], f"{data['strm_base']}.strm")
            data["strm_path"] = new_strm
            if old_strm and old_strm != new_strm and os.path.exists(old_strm):
                try:
                    os.makedirs(data["target_dir"], exist_ok=True)
                    shutil.move(old_strm, new_strm)
                    old_p = os.path.dirname(old_strm)
                    if os.path.isdir(old_p) and not os.listdir(old_p):
                        try:
                            os.rmdir(old_p)
                        except OSError:
                            pass
                except Exception:
                    pass
            data["_force_art_refresh"] = True
            cache_updated = True
            logger.info(f"[CACHE-HEAL] Healed Derry from Summer I Turned Pretty collision -> '{new_strm}'")

        # 3. Clean release-size noise from title, clean_title, strm_base
        cur_title = data.get("title") or ""
        cur_clean = data.get("clean_title") or ""
        cur_base = data.get("strm_base") or ""
        if size_pattern.search(cur_title) or size_pattern.search(cur_clean) or size_pattern.search(cur_base):
            meta = parse_media_meta(raw_name)
            if meta.get("title") and meta["title"] != cur_title:
                data["title"] = meta["title"]
                data["clean_title"] = meta["clean_title"]
                data["search_title"] = meta["search_title"]
                data["strm_base"] = meta["clean_title"]
                if meta.get("year"):
                    data["year"] = meta["year"]
                new_tgt = strm_dir(MOVIES_DIR, SHOWS_DIR, data)
                new_strm = os.path.join(new_tgt, f"{data['strm_base']}.strm")
                data["target_dir"] = new_tgt
                data["strm_path"] = new_strm
                cache_updated = True
                logger.info(f"[CACHE-HEAL] Cleaned size noise for '{raw_name}' -> '{meta['title']}'")

    if cache_updated:
        _save_cache_sync()

def _save_cache_sync():
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        tmp = f"{CACHE_FILE}.{os.getpid()}_{uuid.uuid4().hex[:8]}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(FILE_ID_CACHE, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CACHE_FILE)
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
    try:
        m_int = int(message_id)
        if m_int < 0 and chat_id is not None and int(chat_id) > 0:
            message_id, chat_id = int(chat_id), m_int
    except (TypeError, ValueError):
        pass
    if chat_id:
        entry = FILE_ID_CACHE.get(_cache_key(chat_id, message_id))
        if isinstance(entry, dict):
            return entry
        alt_entry = FILE_ID_CACHE.get(_cache_key(message_id, chat_id))
        if isinstance(alt_entry, dict):
            return alt_entry
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
            c = int(chat_part)
            m = int(msg_part)
            if c > 0 and m < 0:
                c, m = m, c
            return c, m
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
        os.makedirs(os.path.dirname(SUBTITLE_CACHE_FILE), exist_ok=True)
        tmp = f"{SUBTITLE_CACHE_FILE}.{os.getpid()}_{uuid.uuid4().hex[:8]}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(SUBTITLE_CACHE, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, SUBTITLE_CACHE_FILE)
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
_METADATA_SEMAPHORE = asyncio.Semaphore(max(1, APEX_CFG.metadata_workers))
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
    dc_id = 0
    file_id = entry.get("file_id")
    if file_id:
        try:
            from pyrogram.file_id import FileId

            dc_id = FileId.decode(file_id).dc_id
        except Exception:
            dc_id = 0
    return FileInfo(
        size=int(entry.get("file_size") or 0),
        name=entry.get("title") or f"Media_{message_id}",
        file_id=file_id,
        mime_type="video/mp4",
        dc_id=dc_id or None,
    )


async def _warm_channel_peer(client, chat_id):
    """Resolve a channel peer Pyrogram's get_peer_type() would reject.

    utils.get_peer_type() only accepts channel ids in the legacy signed-int32
    range, so a fresh session cannot resolve this chat via get_messages() (the
    "ValueError: Peer id invalid" seen for -1003907801136). Pyrogram's own
    resolve_peer() channel branch invokes raw channels.GetChannels with an
    InputChannel(access_hash=0) and lets Client.invoke()'s fetch_peers() write
    the peer (and its real access_hash) into the client storage; afterwards the
    normal get_messages() resolution path succeeds.
    """
    try:
        from pyrogram import raw as _raw
    except Exception:
        return False
    if not (isinstance(chat_id, int) and chat_id < 0):
        return False
    # 1. Try Pyrogram's native get_chat peer warming
    try:
        chat = await client.get_chat(chat_id)
        if chat:
            return True
    except Exception:
        pass
    # 2. Raw channels.GetChannels warming
    channel_id = -chat_id - 1000000000000  # peer_id = MAX_CHANNEL_ID - channel_id
    if channel_id <= 0:
        return False
    try:
        r = await client.invoke(
            _raw.functions.channels.GetChannels(
                id=[_raw.types.InputChannel(channel_id=channel_id, access_hash=0)]
            )
        )
        return bool(getattr(r, "chats", None))
    except Exception as e:
        logger.warning(f"[REFRESH] channel warm failed for {chat_id}: {type(e).__name__}: {e}")
        return False


async def _refresh_file_reference(client, chat_id, message_id):
    """Re-resolve a fresh media file reference.

    Telegram file references are short-lived tokens, so on FILE_REFERENCE_EXPIRED
    the source message must be re-fetched via get_messages() and the fresh
    file_id / file_size written back into FILE_ID_CACHE.
    Tries through `client` first. If `client` lacks channel access (e.g. an extra
    bot not added as an administrator to the channel) or fails, falls back to
    `tg_app` (the primary bot) or other available pool clients so that expired
    references can be recovered without requiring manual reforwarding.
    Returns (file_id, file_size) or None when no client can re-resolve it.
    """
    async def _try_fetch(c):
        if not c:
            return None
        try:
            msg = await c.get_messages(chat_id, message_id)
        except Exception as e:
            logger.warning(
                f"[REFRESH] get_messages({chat_id}, {message_id}) failed on {getattr(c, 'name', '?')}: {type(e).__name__}: {e}"
            )
            if await _warm_channel_peer(c, chat_id):
                try:
                    msg = await c.get_messages(chat_id, message_id)
                except Exception as e2:
                    logger.warning(
                        f"[REFRESH] get_messages({chat_id}, {message_id}) retry failed on {getattr(c, 'name', '?')}: "
                        f"{type(e2).__name__}: {e2}"
                    )
                    return None
            else:
                return None
        m = (
            (msg.video or msg.document or msg.audio or msg.animation)
            if msg else None
        )
        f_id = getattr(m, "file_id", None) if m else None
        if not f_id:
            return None
        return f_id, int(getattr(m, "file_size", 0) or 0)

    # 1. Primary candidate: the requesting client
    res = await _try_fetch(client)
    if res is not None:
        return res

    # 2. High-priority fallback: primary bot client (tg_app)
    tg_app_client = globals().get("tg_app")
    if tg_app_client is not None and tg_app_client is not client and getattr(tg_app_client, "is_connected", False):
        res = await _try_fetch(tg_app_client)
        if res is not None:
            logger.info(f"[REFRESH] recovered fresh reference {chat_id}:{message_id} via primary bot client")
            return res

    # 3. Fallback: user client (tg_user) if present
    tg_user_client = globals().get("tg_user")
    if tg_user_client is not None and tg_user_client is not client and getattr(tg_user_client, "is_connected", False):
        res = await _try_fetch(tg_user_client)
        if res is not None:
            logger.info(f"[REFRESH] recovered fresh reference {chat_id}:{message_id} via user client")
            return res

    # 4. Fallback: any other connected extra clients
    for _, extra_c in globals().get("extra_clients", []):
        if extra_c is not client and extra_c is not tg_app_client and getattr(extra_c, "is_connected", False):
            res = await _try_fetch(extra_c)
            if res is not None:
                logger.info(f"[REFRESH] recovered fresh reference {chat_id}:{message_id} via extra client {getattr(extra_c, 'name', '?')}")
                return res

    logger.warning(
        f"[REFRESH] message {chat_id}:{message_id} no longer carries fresh media on any client"
    )
    return None


def _is_expired_reference(exc):
    """True when `exc` is Telegram's FILE_REFERENCE_EXPIRED (any pyrogram layout).

    Pyrogram has historically raised the pascal-cased ``FileReferenceExpired``
    (an ``RPCError`` with ``ID == "FILE_REFERENCE_EXPIRED"``), but the symbol is
    exported at different paths across versions/builds, so match defensively:
    the concrete class when importable, then the RPC error ID, class name, raw
    value, or rendered text via an underscore-insensitive comparison.
    """
    if exc is None:
        return False
    if FileReferenceExpired is not None and isinstance(exc, FileReferenceExpired):
        return True
    probes = [
        getattr(exc, "ID", None),
        type(exc).__name__,
        getattr(exc, "value", None),
        str(exc),
    ]
    if getattr(exc, "__cause__", None):
        probes.append(str(exc.__cause__))
    if getattr(exc, "__context__", None):
        probes.append(str(exc.__context__))
    for probe in probes:
        if not probe:
            continue
        normalized = str(probe).upper().replace("_", "")
        if "FILEREFERENCEEXPIRED" in normalized:
            return True
    return False


async def _media_session_for(client, dc_id):
    """Return a session authorized on ``dc_id``, creating + caching it once per
    (client, dc) like Pyrogram's ``inline_session.get_session`` (export auth at
    most once per account). Same-DC file requests get the main client itself.

    This is the fix for the ``auth.ExportAuthorization`` FLOOD_WAIT: Pyrogram's
    ``Client.get_file`` builds a brand-new Session and re-runs ExportAuthorization
    on EVERY call, but a cached media session exports exactly once.
    """
    if dc_id == await client.storage.dc_id():
        return client
    lock = getattr(client, "media_sessions_lock", None)
    sessions = getattr(client, "media_sessions", None)
    if lock is None or sessions is None:
        return client
    from pyrogram import raw as _raw
    from pyrogram.errors import AuthBytesInvalid
    from pyrogram.session import Auth, Session

    async with lock:
        existing = sessions.get(dc_id)
        if existing:
            return existing
        session = sessions[dc_id] = Session(
            client, dc_id,
            await Auth(client, dc_id, await client.storage.test_mode()).create(),
            await client.storage.test_mode(), is_media=True
        )
        try:
            await session.start()
            for _ in range(3):
                exported_auth = await client.invoke(_raw.functions.auth.ExportAuthorization(dc_id=dc_id))
                try:
                    await session.invoke(_raw.functions.auth.ImportAuthorization(id=exported_auth.id, bytes=exported_auth.bytes))
                except AuthBytesInvalid:
                    continue
                else:
                    break
            else:
                raise AuthBytesInvalid
        except Exception:
            # don't leave a started-but-unauthorized session cached; a later
            # call (this client or a failover) must be able to re-export
            sessions.pop(dc_id, None)
            try:
                await session.stop()
            except Exception:
                pass
            raise
        return session


async def _stream_file_chunks(client, file_id, run_start, chunk_count):
    """Yield 1 MiB ``upload.GetFile`` chunks over a cached media session.

    Mirrors Pyrogram's ``get_file`` transport (location shape, CDN decrypt)
    WITHOUT its error-swallowing: FILE_REFERENCE_EXPIRED / FLOOD_WAIT / transport
    errors propagate so the driver can refresh-and-replay or fail over to another
    (non-flooded) pool client.
    """
    from hashlib import sha256

    from pyrogram import raw as _raw
    from pyrogram import utils as _utils
    from pyrogram.crypto import aes
    from pyrogram.errors import CDNFileHashMismatch, VolumeLocNotFound
    from pyrogram.file_id import FileId, FileType, ThumbnailSource
    from pyrogram.session import Auth, Session

    fid = FileId.decode(file_id)

    if fid.file_type == FileType.CHAT_PHOTO:
        if fid.chat_id > 0:
            peer = _raw.types.InputPeerUser(user_id=fid.chat_id, access_hash=fid.chat_access_hash)
        else:
            if fid.chat_access_hash == 0:
                peer = _raw.types.InputPeerChat(chat_id=-fid.chat_id)
            else:
                peer = _raw.types.InputPeerChannel(
                    channel_id=_utils.get_channel_id(fid.chat_id),
                    access_hash=fid.chat_access_hash
                )
        location = _raw.types.InputPeerPhotoFileLocation(
            peer=peer,
            photo_id=fid.media_id,
            big=fid.thumbnail_source == ThumbnailSource.CHAT_PHOTO_BIG
        )
    elif fid.file_type == FileType.PHOTO:
        location = _raw.types.InputPhotoFileLocation(
            id=fid.media_id,
            access_hash=fid.access_hash,
            file_reference=fid.file_reference,
            thumb_size=fid.thumbnail_size
        )
    else:
        location = _raw.types.InputDocumentFileLocation(
            id=fid.media_id,
            access_hash=fid.access_hash,
            file_reference=fid.file_reference,
            thumb_size=fid.thumbnail_size
        )

    session = await _media_session_for(client, fid.dc_id)
    chunk_size = 1024 * 1024
    offset_bytes = run_start * chunk_size
    total = chunk_count
    current = 0

    r = await session.invoke(
        _raw.functions.upload.GetFile(location=location, offset=offset_bytes, limit=chunk_size),
        sleep_threshold=30
    )

    if isinstance(r, _raw.types.upload.File):
        while True:
            chunk = r.bytes
            yield chunk
            current += 1
            if len(chunk) < chunk_size or current >= total:
                break
            offset_bytes += chunk_size
            r = await session.invoke(
                _raw.functions.upload.GetFile(location=location, offset=offset_bytes, limit=chunk_size),
                sleep_threshold=30
            )
    elif isinstance(r, _raw.types.upload.FileCdnRedirect):
        cdn_session = Session(
            client, r.dc_id,
            await Auth(client, r.dc_id, await client.storage.test_mode()).create(),
            await client.storage.test_mode(), is_media=True, is_cdn=True
        )
        try:
            await cdn_session.start()
            while True:
                r2 = await cdn_session.invoke(
                    _raw.functions.upload.GetCdnFile(file_token=r.file_token, offset=offset_bytes, limit=chunk_size)
                )
                if isinstance(r2, _raw.types.upload.CdnFileReuploadNeeded):
                    try:
                        await session.invoke(
                            _raw.functions.upload.ReuploadCdnFile(file_token=r.file_token, request_token=r2.request_token)
                        )
                    except VolumeLocNotFound:
                        break
                    else:
                        continue

                chunk = r2.bytes
                decrypted_chunk = aes.ctr256_decrypt(
                    chunk,
                    r.encryption_key,
                    bytearray(r.encryption_iv[:-4] + (offset_bytes // 16).to_bytes(4, "big"))
                )
                hashes = await session.invoke(
                    _raw.functions.upload.GetCdnFileHashes(file_token=r.file_token, offset=offset_bytes)
                )
                for i, h in enumerate(hashes):
                    cdn_chunk = decrypted_chunk[h.limit * i:h.limit * (i + 1)]
                    CDNFileHashMismatch.check(h.hash == sha256(cdn_chunk).digest(), "h.hash == sha256(cdn_chunk).digest()")

                yield decrypted_chunk
                current += 1
                if len(chunk) < chunk_size or current >= total:
                    break
                offset_bytes += chunk_size
        finally:
            await cdn_session.stop()


class _MediaTransport:
    """Instance-level ``stream_media(file_id, offset=, limit=)`` replacement that
    serves chunks over a cached media session (export auth once). Installed on
    every pool client so ``_fetch_chunks`` keeps its ``client.stream_media(...)``
    call site while never re-running ExportAuthorization per run."""

    def __init__(self, client):
        self._client = client

    def __call__(self, file_id, offset=0, limit=0):
        return _stream_file_chunks(self._client, file_id, offset, limit)


def _install_media_transport(client):
    try:
        client.stream_media = _MediaTransport(client)
    except Exception as e:
        logger.warning(f"[MEDIA] could not install cached-session transport on {type(client).__name__}: {type(e).__name__}: {e}")


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
        # Another concurrent range request may already have refreshed this
        # source while this request was reading its previous reference.
        latest = _cache_lookup(message_id, chat_id)
        if isinstance(latest, dict) and latest.get("file_id") != file_id:
            file_id = latest.get("file_id")
            stored_size = int(latest.get("file_size") or 0)
            if stored_size:
                remaining = stored_size - run_start * chunk_bytes
                run_expected = min(run_expected, max(remaining, 0))
            continue
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

# Extra Telegram bots (JELLYFIN_MULTI_TOKEN1... or APEX_TELEGRAM_EXTRA_TOKENS):
# started solely for streaming;
# they never consume updates so the webhook/primary bot owns ingestion.
extra_clients = []
if not APEX_CFG.extra_tokens:
    discovered_tokens = []
    for index in range(1, 100):
        if val := os.environ.get(f"JELLYFIN_MULTI_TOKEN{index}", "").strip():
            if val not in discovered_tokens:
                discovered_tokens.append(val)
    for prefix in ("BOT_TOKEN_", "TG_BOT_TOKEN_", "MULTI_TOKEN_"):
        for index in range(1, 25):
            if val := os.environ.get(f"{prefix}{index}", "").strip():
                if val not in discovered_tokens and val != BOT_TOKEN:
                    discovered_tokens.append(val)
    for env_name in ("EXTRA_BOT_TOKENS", "MULTI_BOT_TOKENS", "APEX_EXTRA_TOKENS", "APEX_TELEGRAM_EXTRA_TOKENS"):
        if val := os.environ.get(env_name, "").strip():
            for tok in val.split(","):
                tok_clean = tok.strip()
                if tok_clean and tok_clean not in discovered_tokens and tok_clean != BOT_TOKEN:
                    discovered_tokens.append(tok_clean)
    APEX_CFG.extra_tokens = discovered_tokens
    if APEX_CFG.extra_tokens:
        logger.info(f"[POOL] Discovered {len(APEX_CFG.extra_tokens)} extra streaming bot token(s).")
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
    if is_blacklisted(file_name):
        logger.info(f"[BLACKLIST] Refusing to index blacklisted media: {file_name}")
        return False

    entry = _meta_to_entry(msg_id, chat_id, file_id, file_size, file_name)
    cached = _cache_lookup(msg_id, chat_id)
    if isinstance(cached, dict):
        # Keep durable runtime fields, but re-parse when Telegram now exposes
        # a better filename/caption than the cached generic name. This repairs
        # older Unknown_Media entries during restore/reindex.
        same_name = (cached.get("file_name") or "").strip() == (file_name or "").strip()
        if same_name:
            entry.update(cached)
        else:
            for key in (
                "tmdb_id", "tmdb_title", "tmdb_year",
            ):
                if key in cached:
                    entry[key] = cached[key]
        entry.update({
            "file_id": file_id,
            "file_size": file_size,
            "file_name": file_name,
            "msg_id": msg_id,
            "chat_id": chat_id,
        })

    record = _catalog_upsert(entry)
    confidence = _ingestion_confidence(entry)
    if confidence < INGESTION_REVIEW_THRESHOLD:
        _record_review_job(entry, confidence, "unidentified_or_low_confidence")
        logger.info(f"[INGEST] Queued low-confidence media for review: {file_name!r}")
        return False
    if record is not None and record.deleted:
        # A new Telegram upload is an explicit request to publish again.
        MEDIA_CATALOG.unpublish(
            source_chat_id=int(chat_id or 0),
            source_message_id=int(msg_id),
        )
        entry["catalog_deleted"] = False

    expected = entry.get("strm_path")
    if not expected:
        expected = _entry_strm_path(entry)
    if isinstance(cached, dict) and cached.get("file_id") == file_id and os.path.exists(expected):
        return cached.get("title") or True

    FILE_ID_CACHE[_cache_key(chat_id, msg_id)] = entry
    save_cache()

    strm_name = create_strm_file(entry)
    logger.info(f"[INGEST] Media indexed from Telegram: {strm_name} (chat={chat_id})")
    return strm_name


def media_file_name(message, media):
    filename = (getattr(media, "file_name", None) or "").strip()
    caption = (getattr(message, "caption", None) or "").strip()
    generic = not filename or re.fullmatch(
        r"(?:telegram[_ -])?media[_ -]?\d+(?:\.[a-z0-9]+)?", filename, re.IGNORECASE
    )
    if caption and generic:
        return caption
    return filename or caption or f"Telegram_Media_{message.id}"


def allowed_chat(chat) -> bool:
    if not RAW_CHANNEL_ID:
        return True
    allowed = [a.strip() for a in RAW_CHANNEL_ID.split(",") if a.strip()]
    if not allowed:
        return True
    if chat and chat.type == ChatType.PRIVATE:
        return True
    return str(chat.id) in allowed


VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm", ".ts", ".m4v", ".3gp", ".mpeg", ".mpg"
}


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
        caption = getattr(message, "caption", None)
        if is_blacklisted(file_name) or is_blacklisted(caption):
            logger.info(f"[BLACKLIST] Dropped blacklisted media message: {file_name}")
            return
        if subtitle_is_document(file_name, getattr(media, "mime_type", None)):
            placed = await _place_subtitle(message.id, chat_id, media.file_id, file_name)
            logger.info(f"[PYROGRAM] 🎞️ Subtitle handled: {file_name} placed={placed}")
            return

        # Ensure media is an actual video file (not zip/rar/apk/audio/sample clip)
        ext = os.path.splitext(file_name)[1].lower()
        if ext and ext not in VIDEO_EXTENSIONS:
            logger.info(f"[MEDIA-SKIP] Dropped non-video file: {file_name} ({ext})")
            return

        file_size = int(getattr(media, "file_size", 0) or 0)
        # Drop preview clips and promotional samples (< 25MB with 'sample' in name, or < 10MB)
        if file_size < 10 * 1024 * 1024 or (file_size < 25 * 1024 * 1024 and "sample" in file_name.lower()):
            logger.info(f"[SAMPLE-DROP] Dropped short video sample/clip: {file_name} ({file_size} bytes)")
            return

        strm_name = index_media(message.id, chat_id, media.file_id, file_size, file_name)
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
        try:
            from Thunder.bot.plugins.ai_admin import setup_ai_admin
            setup_ai_admin(tg_app)
        except Exception as _ai_e:
            logger.debug(f"[AI-ADMIN] Could not wire AI Admin to tg_app: {_ai_e}")
    except Exception as e:
        logger.error(f"[PYROGRAM] Error initializing Pyrogram: {e}")

if tg_app:
    @tg_app.on_message()
    async def on_any_update(client, message):
        chat = message.chat
        chat_t = getattr(chat, "type", None)
        logger.info(f"[PYROGRAM][RX] Received update: chat={chat.id if chat else '?'} type={chat_t} media={getattr(message, 'media', None)} caption={str(message.caption or '')[:40]!r}")
        try:
            message.continue_propagation()
        except Exception:
            pass

    @tg_app.on_message(filters.command(["delete", "purge", "remove"]))
    async def on_delete_command(client, message):
        chat = message.chat
        if not allowed_chat(chat):
            logger.warning(f"[DELETE-CMD] Unauthorized chat attempted delete: {getattr(chat, 'id', None)}")
            return

        reply = getattr(message, "reply_to_message", None)
        target_title = None
        purged = []

        if reply:
            target_msg_id = getattr(reply, "id", None)
            target_chat_id = getattr(reply.chat, "id", None) if reply.chat else None
            purged = await purge_media_item(chat_id=target_chat_id, msg_id=target_msg_id)
        else:
            parts = (getattr(message, "text", "") or "").split(maxsplit=1)
            if len(parts) > 1:
                target_title = parts[1].strip()
                purged = await purge_media_item(title_query=target_title)
            else:
                try:
                    await message.reply_text(
                        "⚠️ **Usage:**\n"
                        "• Reply to a video post with `/delete`\n"
                        "• Or type `/delete <Movie Title>` (e.g. `/delete Kattalan`)"
                    )
                except Exception:
                    pass
                return

        if purged:
            try:
                await message.reply_text(
                    f"✅ **Permanently removed:**\n"
                    f"{', '.join(purged)}\n\n"
                    f"The media has been deleted from Jellyfin and disk, and will not return on restart."
                )
            except Exception:
                pass
        else:
            query_desc = f"'{target_title}'" if target_title else "this message"
            try:
                await message.reply_text(
                    f"❌ Could not find any active media in the library matching {query_desc}."
                )
            except Exception:
                pass

    @tg_app.on_message(filters.video | filters.document | filters.audio | filters.animation)
    async def on_media_message(client, message):
        is_channel_post = bool(message.chat and message.chat.type == ChatType.CHANNEL)
        await process_telegram_media(message, is_channel_post)

    if hasattr(tg_app, "on_deleted_messages"):
        @tg_app.on_deleted_messages()
        async def on_deleted_media_messages(client, messages):
            logger.info(f"[PYROGRAM][DELETE] Received deleted messages event ({len(messages)} messages)")
            for msg in messages:
                try:
                    msg_id = getattr(msg, "id", None)
                    chat = getattr(msg, "chat", None)
                    chat_id = getattr(chat, "id", None) if chat else None
                    if not msg_id:
                        continue
                    purged = await purge_media_item(chat_id=chat_id, msg_id=msg_id)
                    if purged:
                        logger.info(f"[PYROGRAM][DELETE] Automatically purged deleted Telegram media: {purged}")
                except Exception as e:
                    logger.error(f"[PYROGRAM][DELETE] Error handling deleted message: {e}")

    logger.info("[PYROGRAM] Message, command & deletion handlers registered.")

# Optional Pyrogram User Account Client (bypasses bot restrictions on reading past channel history)
tg_user = None
if API_ID and API_HASH and USER_SESSION_STRING:
    try:
        tg_user = Client(
            "tg_user_session",
            api_id=int(API_ID),
            api_hash=API_HASH,
            session_string=USER_SESSION_STRING,
            workdir=DATA_DIR,
            in_memory=True,
            no_updates=True,
            max_concurrent_transmissions=APEX_CFG.telegram_max_concurrent,
        )
        logger.info("[PYROGRAM] Pyrogram user account client initialized.")
    except Exception as e:
        logger.error(f"[PYROGRAM] Error initializing Pyrogram user client: {e}")

logger.info(
    "[CONFIG] Jellyfin Telegram primary bot: api_id=%s api_hash=%s bot_token=%s "
    "allowed_chat_ids=%s tmdb_api_key=%s",
    "set" if API_ID else "missing",
    "set" if API_HASH else "missing",
    "set" if BOT_TOKEN else "missing",
    "configured" if RAW_CHANNEL_ID else "not configured (all accessible chats)",
    "set" if TMDB_API_KEY else "missing",
)

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
        "tmdb_configured": bool(TMDB_API_KEY),
        "pending_subtitles": sum(1 for s in SUBTITLE_CACHE.values() if isinstance(s, dict) and not s.get("placed")),
        "placed_subtitles": sum(1 for s in SUBTITLE_CACHE.values() if isinstance(s, dict) and s.get("placed")),
        "movies_dir": MOVIES_DIR,
        "shows_dir": SHOWS_DIR,
        "organization": "per-title (Movies/<Language>/<Title> (<Year>), TV Shows/<Language>/<Show> (<Year>)/Season NN) + TMDB nfo/poster/backdrop",
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
    meta = parse_media_meta(filename_or_caption)
    return meta["is_tv"], meta["title"], meta["show_name"], meta["season"], meta["episode"]

async def _tmdb_get(path, params):
    """GET https://api.themoviedb.org/3{path}?{params}&api_key=..."""
    if not TMDB_API_KEY:
        return None
    qs = urllib.parse.urlencode({"api_key": TMDB_API_KEY, **params})
    url = f"https://api.themoviedb.org/3{path}?{qs}"
    try:
        connector = aiohttp.TCPConnector(family=socket.AF_INET)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.warning(f"[TMDB] GET {path} -> {resp.status}")
    except Exception as e:
        logger.warning(f"[TMDB] request notice for '{path}': {e}")
    return None


async def _tmdb_search(media_type, query, year=None, language=None):
    """Search TMDB for a movie or TV series; returns a list of candidate hits.

    `language` (ISO 639-1) biases results toward that language's originals,
    which matters for transliterated/native-script queries ("aadu 2 2023 Malayalam"
    without the year keeps matching the right Malayalam film).
    """
    params = {"query": query or "", "include_adult": "false"}
    if language:
        params["language"] = language

    results = []
    if year:
        yparams = dict(params)
        if media_type == "tv":
            yparams["first_air_date_year"] = int(year)
        else:
            yparams["year"] = int(year)
        data = await _tmdb_get(f"/search/{media_type}", yparams)
        results = (data.get("results") or []) if data else []

    # Also query without strict year filter so releases where festival premiere year
    # vs theatrical release year differ by 1 year (e.g. Obsession 2025 festival vs 2026 theatrical)
    # are included in candidate hits for best_tmdb_match to rank.
    data_all = await _tmdb_get(f"/search/{media_type}", params)
    all_results = (data_all.get("results") or []) if data_all else []

    seen = set()
    merged = []
    for r in results + all_results:
        rid = r.get("id")
        if rid and rid not in seen:
            seen.add(rid)
            merged.append(r)
    return merged


async def _tmdb_find(external_id):
    """Resolve an external id (imdb_id, tvdb_id) through TMDB /find.

    Deterministic when a release filename embeds the id (Radarr-style
    "{imdb-tt12345678}"), so no fuzzy text search is needed. Returns a list of
    movie/tv hits tagged with their `media_type`.
    """
    data = await _tmdb_get(
        f"/find/{external_id}", {"external_source": "imdb_id"}
    )
    results = []
    for r in (data.get("movie_results") or []) if data else []:
        r = dict(r)
        r["media_type"] = "movie"
        results.append(r)
    for r in (data.get("tv_results") or []) if data else []:
        r = dict(r)
        r["media_type"] = "tv"
        results.append(r)
    return results


async def _fetch_imdb_suggestion(query: str, year: Optional[int] = None, media_type: str = "movie") -> Optional[Dict[str, Any]]:
    """Query official IMDb suggestion API with composite disambiguation scoring."""
    if not query:
        return None
    clean = re.sub(r"[^a-zA-Z0-9]+", "_", query).strip("_").lower()
    if not clean:
        return None
    url = f"https://v3.sg.media-imdb.com/suggestion/x/{clean}.json"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        connector = aiohttp.TCPConnector(family=socket.AF_INET)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(url, headers=headers, timeout=8) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    suggestions = data.get("d") or []
                    target_tokens = set(re.findall(r"[a-z0-9]+", query.lower()))
                    target_str = " ".join(re.findall(r"[a-z0-9]+", query.lower()))
                    candidates = []
                    for idx, s in enumerate(suggestions):
                        sid = s.get("id")
                        if not sid or not sid.startswith("tt"):
                            continue
                        stype = s.get("qid") or s.get("q") or ""
                        if media_type == "tv" and stype not in ("tvSeries", "tvMiniSeries", "series"):
                            continue
                        if media_type == "movie" and stype in ("tvSeries", "tvMiniSeries", "series"):
                            continue

                        s_year = s.get("y")
                        s_raw = s.get("l") or ""
                        s_tokens = set(re.findall(r"[a-z0-9]+", s_raw.lower()))
                        s_norm = " ".join(re.findall(r"[a-z0-9]+", s_raw.lower()))

                        # 1. Punctuation-agnostic Title Match
                        if s_norm == target_str:
                            title_score = 3.0
                        elif target_tokens and target_tokens.issubset(s_tokens):
                            extra = len(s_tokens - target_tokens)
                            title_score = 2.0 - min(1.0, extra * 0.2)
                        elif target_tokens and s_tokens and len(target_tokens & s_tokens) / len(target_tokens) >= 0.6:
                            title_score = 1.0
                        else:
                            title_score = 0.0

                        if title_score <= 0.0:
                            continue

                        # 2. Year alignment score with strict movie vs tv validation
                        if year and s_year:
                            try:
                                sy = int(s_year)
                                y = int(year)
                                if media_type == "movie":
                                    ydiff = abs(sy - y)
                                    if ydiff == 0:
                                        year_score = 2.0
                                    elif ydiff == 1:
                                        year_score = 0.5
                                    else:
                                        # Strict movie rule: >=2 year gap is rejected to prevent mismatched films
                                        year_score = -5.0
                                else:
                                    # TV Series: season release year (y) is normally >= series premiere year (sy)
                                    if y >= sy:
                                        year_score = 1.5 if (y - sy) <= 3 else 0.5
                                    else:
                                        # Series cannot premiere in the future compared to file year
                                        year_score = -5.0
                            except (ValueError, TypeError):
                                year_score = 0.0
                        else:
                            year_score = 0.0

                        # 3. Prominence score (IMDb native rank order)
                        rank_score = max(0.0, 0.5 - (idx * 0.08))
                        # 4. Poster availability bonus
                        has_img = 0.2 if (s.get("i") or {}).get("imageUrl") else 0.0

                        total_score = title_score + year_score + rank_score + has_img
                        # Strict confidence gate: require at least 3.2 total score
                        if total_score >= 3.2:
                            candidates.append((total_score, idx, s))

                    if candidates:
                        candidates.sort(key=lambda c: (c[0], -c[1]), reverse=True)
                        # Ambiguity detection: if top 2 candidates are virtually tied (< 0.15 diff)
                        if len(candidates) > 1:
                            top_score = candidates[0][0]
                            second_score = candidates[1][0]
                            if (top_score - second_score) < 0.15 and candidates[0][2].get("id") != candidates[1][2].get("id"):
                                logger.info(
                                    f"[IMDB-SUGGEST] Ambiguous candidates for '{query}' "
                                    f"('{candidates[0][2].get('l')}' {top_score:.2f} vs '{candidates[1][2].get('l')}' {second_score:.2f}); held for review"
                                )
                                return None
                        best = candidates[0][2]
                        logger.info(f"[IMDB-SUGGEST] Matched '{query}' ({year or 'any'}) -> '{best.get('l')}' ({best.get('y')}) [{best.get('id')}] (score={candidates[0][0]:.2f})")
                        return best
    except Exception as e:
        logger.debug(f"[IMDB-SUGGEST] Notice for '{query}': {e}")
    return None


async def _fetch_cinemeta_meta(imdb_id: str, media_type: str = "movie") -> Optional[Dict[str, Any]]:
    """Query free Cinemeta (Stremio IMDb catalog) for full plot, genres, and artwork."""
    if not imdb_id or not imdb_id.startswith("tt"):
        return None
    ctype = "series" if media_type == "tv" else "movie"
    url = f"https://v3-cinemeta.strem.io/meta/{ctype}/{imdb_id}.json"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        connector = aiohttp.TCPConnector(family=socket.AF_INET)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(url, headers=headers, timeout=8) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("meta")
    except Exception as e:
        logger.debug(f"[CINEMETA] Notice for '{imdb_id}': {e}")
    return None


_AI_TITLE_SYSTEM = (
    "You resolve the REAL official title of movies and TV series from Telegram "
    "release filenames, including Indian (Malayalam, Tamil, Telugu, Hindi, "
    "Kannada) and international OTT releases. Strip ALL release junk: "
    "extensions (.mkv/.mp4), codecs (DD+5.1, DTS-MA, 1080p, x264, x265, WEB-DL), "
    "resolution/channel/sample markers, OTT brands (JioHotstar, Hotstar, "
    "JioCinema, SonyLiv, Zee5, MXPlayer), uploader group tags, languages, and "
    "trailing jabber. Then use your knowledge of films to recover the exact "
    "official title even when the release name is mangled or abbreviated "
    "(e.g. \"Mollywood Times Jhs\" -> \"Mollywood Times\"; \"Obsession 6CH\" "
    "-> \"Obsession\"). Return the title exactly as TMDB/IMDb list it, with the "
    "real release year. CRITICAL: NEVER replace an Indian regional release "
    "(Malayalam, Tamil, Telugu, etc.) or a modern film (2024-2026) with an "
    "unrelated old Hollywood/English film (e.g. from the 1990s or 2000s). For example, "
    "'Patriot Movie 2026 malayalam' is the 2026 Malayalam movie 'Patriot', NEVER the "
    "2000 movie 'The Patriot'. Return a strict JSON object "
    "{\"title\": string|null, \"year\": int|null}. Only the JSON object, no "
    "explanations, no markdown fences."
)


async def _ai_extract_title(filename):
    """Use the omniroute LLM proxy to extract the canonical title+year.

    Returns (title, year) or (None, None). Never raises; on any proxy/parse
    failure it logs a warning and returns None so ingestion always proceeds.
    """
    if not (OMNIROUTE_URL and OMNIROUTE_KEY):
        return None, None
    key = filename or ""
    if key in _AI_TITLE_CACHE:
        return _AI_TITLE_CACHE[key]
    connector = aiohttp.TCPConnector(family=socket.AF_INET)
    payload = {
        "model": AI_TITLE_MODEL,
        "temperature": 0,
        "max_tokens": AI_TITLE_MAX_TOKENS,
        "messages": [
            {"role": "system", "content": _AI_TITLE_SYSTEM},
            {"role": "user", "content": filename or ""},
        ],
    }
    headers = {
        "Authorization": f"Bearer {OMNIROUTE_KEY}",
        "Content-Type": "application/json",
    }
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.post(
                    f"{OMNIROUTE_URL}/chat/completions",
                    json=payload,
                    headers=headers,
                    timeout=45,
                ) as resp:
                    if resp.status != 200:
                        body = (await resp.text())[:200] or ""
                        logger.warning(
                            f"[AI-TITLE] proxy {resp.status} for '{filename}'"
                            f"{f': {body}' if body else ''}"
                        )
                        _AI_TITLE_CACHE[key] = (None, None)
                        return None, None
                    data = await resp.json()
            content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            stripped = content.strip()
            # Tolerate ```json ... ``` fences the model sometimes wraps output in.
            if stripped.startswith("```"):
                stripped = re.sub(r"^```[a-z]*\s*", "", stripped)
                stripped = re.sub(r"\s*```$", "", stripped)
            try:
                parsed = json.loads(stripped)
            except Exception:
                m = re.search(r"\{.*\}", stripped, re.DOTALL)
                parsed = json.loads(m.group(0)) if m else {}
            title = (parsed.get("title") or "").strip() or None
            year_v = parsed.get("year")
            year = int(year_v) if isinstance(year_v, int) or (isinstance(year_v, str) and str(year_v).isdigit()) else None
            if not title:
                logger.warning(f"[AI-TITLE] empty extraction for '{filename}': {stripped[:160]}")
            _AI_TITLE_CACHE[key] = (title, year)
            return title, year
        except Exception as e:
            logger.warning(f"[AI-TITLE] call notice for '{filename}' (attempt {attempt + 1}/3): {e}")
            await asyncio.sleep(1)
    _AI_TITLE_CACHE[key] = (None, None)
    return None, None


_AI_DISAMBIGUATE_SYSTEM = (
    "You are an expert film and television media disambiguator. Given a raw media release filename or "
    "query context, and a JSON list of candidate TMDB search results, determine the exact TMDB candidate ID "
    "that matches the media.\n"
    "Disambiguation criteria:\n"
    "1. Match against release keywords, language (e.g. Malayalam, Tamil, Telugu, Hindi, Kannada, English), "
    "director, cast, and release year (allow ±1 year difference for festival premieres vs theatrical releases, "
    "e.g. 2025 festival vs 2026 general release).\n"
    "2. NEVER match a modern Indian or regional film to an unrelated classic or English/Hollywood film "
    "(e.g., 'Patriot 2026 Malayalam' matches 2026 Malayalam 'Patriot', NEVER the 2000 English movie 'The Patriot').\n"
    "3. Prefer popular, officially distributed titles with posters/overviews over zero-vote placeholders.\n"
    "4. If no candidate in the list is a genuine match, return null.\n"
    "Return strict JSON only in this format: {\"tmdb_id\": <integer id or null>, \"reason\": \"<short explanation>\"}. "
    "No markdown fences, no explanations outside JSON."
)


async def _ai_disambiguate_candidates(raw_context, candidates, media_type="movie", year=None, language=None):
    """Use Omniroute LLM proxy to disambiguate multiple TMDB candidates.

    Returns the matching candidate dictionary from candidates, or None if no match / failure.
    Never raises; automatically fails safe to None so caller immediately falls back
    to deterministic best_tmdb_match.
    """
    if not (OMNIROUTE_URL and OMNIROUTE_KEY) or not candidates or len(candidates) <= 1:
        return None

    cache_key = f"{raw_context}:{media_type}:{year}:{language}:{len(candidates)}"
    if cache_key in _AI_DISAMBIGUATE_CACHE:
        cached_id = _AI_DISAMBIGUATE_CACHE[cache_key]
        if cached_id is None:
            return None
        for c in candidates:
            if c.get("id") == cached_id:
                return c
        return None

    summaries = []
    for c in candidates[:8]:
        date = (c.get("release_date") or c.get("first_air_date") or "")[:4]
        overview = (c.get("overview") or "").strip()
        if len(overview) > 160:
            overview = overview[:157] + "..."
        summaries.append({
            "id": c.get("id"),
            "title": c.get("title") or c.get("name"),
            "original_title": c.get("original_title") or c.get("original_name"),
            "year": date,
            "original_language": c.get("original_language"),
            "popularity": round(float(c.get("popularity") or 0), 1),
            "overview": overview,
        })

    user_prompt = json.dumps({
        "release_context": raw_context,
        "media_type": media_type,
        "year_hint": year,
        "language_hint": language,
        "candidates": summaries,
    }, ensure_ascii=False)

    connector = aiohttp.TCPConnector(family=socket.AF_INET)
    payload = {
        "model": AI_TITLE_MODEL,
        "temperature": 0,
        "max_tokens": 256,
        "messages": [
            {"role": "system", "content": _AI_DISAMBIGUATE_SYSTEM},
            {"role": "user", "content": user_prompt},
        ],
    }
    headers = {
        "Authorization": f"Bearer {OMNIROUTE_KEY}",
        "Content-Type": "application/json",
    }
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.post(
                f"{OMNIROUTE_URL}/chat/completions",
                json=payload,
                headers=headers,
                timeout=12,
            ) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:160] or ""
                    logger.warning(f"[AI-DISAMBIGUATE] proxy {resp.status} for '{raw_context}': {body}")
                    _AI_DISAMBIGUATE_CACHE[cache_key] = None
                    return None
                data = await resp.json()

        content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        stripped = content.strip()
        if stripped.startswith("```"):
            stripped = re.sub(r"^```[a-z]*\s*", "", stripped)
            stripped = re.sub(r"\s*```$", "", stripped)
        try:
            parsed = json.loads(stripped)
        except Exception:
            m = re.search(r"\{.*\}", stripped, re.DOTALL)
            parsed = json.loads(m.group(0)) if m else {}

        chosen_id = parsed.get("tmdb_id")
        if chosen_id is not None:
            try:
                chosen_id = int(chosen_id)
                for cand in candidates:
                    if cand.get("id") == chosen_id:
                        _AI_DISAMBIGUATE_CACHE[cache_key] = chosen_id
                        logger.info(
                            f"[AI-DISAMBIGUATE] '{raw_context}' -> TMDB ID {chosen_id} "
                            f"('{cand.get('title') or cand.get('name')}', reason: {parsed.get('reason', 'matched')})"
                        )
                        return cand
            except (ValueError, TypeError):
                pass
    except Exception as e:
        logger.warning(f"[AI-DISAMBIGUATE] call notice for '{raw_context}': {e}")

    _AI_DISAMBIGUATE_CACHE[cache_key] = None
    return None


async def _download_image(session, url, save_path):
    """Download an image (e.g. TMDB poster/backdrop) to save_path with size & corruption guards."""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        async with session.get(url, headers=headers, timeout=15) as resp:
            if resp.status == 200:
                data = await resp.read()
                if len(data) < 1000:
                    logger.warning(f"[TMDB] Image from '{url}' too small ({len(data)} bytes), skipping")
                    return False
                if os.path.exists(save_path) and os.path.getsize(save_path) >= 15000 and len(data) < 15000:
                    logger.warning(
                        f"[TMDB] Refusing to overwrite existing {os.path.getsize(save_path)}-byte artwork with {len(data)}-byte payload"
                    )
                    return False
                tmp_path = f"{save_path}.tmp.{os.getpid()}"
                with open(tmp_path, "wb") as f:
                    f.write(data)
                os.replace(tmp_path, save_path)
                return True
    except Exception as e:
        logger.warning(f"[TMDB] image download notice for '{url}': {e}")
    return False


def _generate_fallback_backdrop(poster_path: str, backdrop_path: str) -> bool:
    """Generate a crisp 1920x1080 cinematic backdrop from poster when TMDB lacks one."""
    if os.path.exists(backdrop_path) and (_image_is_hd(backdrop_path, _BACKDROP_MIN_WIDTH) or os.path.getsize(backdrop_path) >= 15000):
        return True
    try:
        from PIL import Image
        if not os.path.exists(poster_path) or os.path.getsize(poster_path) < 1000:
            return False
        with Image.open(poster_path) as img:
            img = img.convert("RGB")
            target_w, target_h = 1920, 1080
            # Scale poster to fill 1920x1080 while keeping crisp 1080p resolution
            bg_scale = max(target_w / img.width, target_h / img.height)
            new_w = int(img.width * bg_scale)
            new_h = int(img.height * bg_scale)
            bg = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
            # Center-crop focused on the upper-mid area (faces & action art)
            left = (new_w - target_w) // 2
            top = max(0, int((new_h - target_h) * 0.25))
            if top + target_h > new_h:
                top = new_h - target_h
            bg = bg.crop((left, top, left + target_w, top + target_h))

            # Apply a smooth cinematic vignette on the left 40% so hero UI text is legible
            mask = Image.new("L", (target_w, target_h), 255)
            mask_data = []
            for y in range(target_h):
                for x in range(target_w):
                    if x < 800:
                        val = int(140 + (x / 800.0) * 115)
                    else:
                        val = 255
                    mask_data.append(val)
            mask.putdata(mask_data)
            dark_bg = Image.new("RGB", (target_w, target_h), (12, 12, 16))
            composite = Image.composite(bg, dark_bg, mask)
            composite.save(backdrop_path, "JPEG", quality=96)
            logger.info(f"[ARTWORK] Generated crisp 1080p cinematic backdrop: {backdrop_path}")
            return True
    except Exception as exc:
        logger.warning(f"[ARTWORK] Fallback backdrop error: {exc}")
        return False


def _nfo_needs_repair(nfo_path):
    """True when an existing NFO's original title looks malformed (non-ASCII
    script collapsed into a run of '?'), so enrichment re-fetches and rewrites
    it instead of treating the NFO as cached."""
    return nfo_needs_repair(nfo_path)


async def _enrich_media_metadata(entry):
    """Fetch full TMDB details for a cache entry and write nfo + poster/backdrop.

    Idempotent: skips when the .nfo + poster already exist and have valid tmdb_id
    so restarts don't hammer the API. Updates the cache entry's tmdb_id/tmdb fields.
    """
    if not isinstance(entry, dict) or not entry.get("title"):
        return False
    key = entry.get("_cache_key") or ""
    try:
        is_tv = bool(entry.get("is_tv"))
        target_dir = entry.get("target_dir")
        show_root = entry.get("show_root")
        if not target_dir:
            logger.info(f"[METADATA] No target_dir for '{entry.get('title')}' - skipping enrich")
            return False

        # Idempotency gate: if metadata is on disk AND has valid identity,
        # avoid re-hitting the API on restart.
        has_identity = bool(
            entry.get("imdb_id") or entry.get("tmdb_id")
            or (isinstance(FILE_ID_CACHE.get(key), dict) and (FILE_ID_CACHE[key].get("imdb_id") or FILE_ID_CACHE[key].get("tmdb_id")))
        )
        known_tmdb = int(entry.get("tmdb_id") or (FILE_ID_CACHE[key].get("tmdb_id") if isinstance(FILE_ID_CACHE.get(key), dict) else 0) or 0)
        force_art = bool(entry.get("_force_art_refresh"))

        if has_identity and not force_art:
            if is_tv and show_root:
                tv_nfo = os.path.join(show_root, "tvshow.nfo")
                tv_backdrop = os.path.join(show_root, "backdrop.jpg")
                has_backdrop = os.path.exists(tv_backdrop) and os.path.getsize(tv_backdrop) >= 15000
                if os.path.exists(tv_nfo) and _poster_art_ok(show_root) and has_backdrop and not _nfo_needs_repair(tv_nfo):
                    return True
            elif not is_tv:
                movie_nfo = os.path.join(
                    target_dir, f"{entry.get('strm_base', strm_base(entry))}.nfo"
                )
                generic_nfo = os.path.join(target_dir, "movie.nfo")
                nfo_file = movie_nfo if os.path.exists(movie_nfo) else generic_nfo
                movie_backdrop = os.path.join(target_dir, "backdrop.jpg")
                has_backdrop = os.path.exists(movie_backdrop) and os.path.getsize(movie_backdrop) >= 15000
                if (os.path.exists(nfo_file) and _poster_art_ok(target_dir)
                        and has_backdrop and not _nfo_needs_repair(nfo_file)):
                    return True

        query = entry.get("search_title") or entry.get("show_name") or entry.get("title")
        candidates = list(entry.get("search_candidates") or [])
        if not candidates or len(candidates) <= 1:
            raw_cand = entry.get("file_name") or entry.get("raw_name") or query or ""
            for fc in _search_candidates(raw_cand):
                if fc not in candidates:
                    candidates.append(fc)
        if query and query not in candidates:
            candidates.insert(0, query)
        if not candidates:
            candidates = [query or ""]

        hit = None
        used_query = ""
        media_type = "tv" if is_tv else "movie"
        lang_code = tmdb_language_code(entry.get("language") or DEFAULT_LANGUAGE)

        # If this item was already verified with a valid TMDB ID, lock directly to that ID
        # NEVER re-run fuzzy text search against candidates for an already verified movie!
        # Reject synthetic or known 404 IDs (e.g. 210879, 5568976, 3215778)
        if known_tmdb and known_tmdb not in (210879, 5568976, 3215778):
            hit = {"id": known_tmdb}
            used_query = f"tmdb:{known_tmdb}"
            logger.info(f"[TMDB-LOCK] Preserved verified identity tmdb_id={known_tmdb} for '{entry.get('title')}'")
        # Tier-0: an embedded IMDb id (Radarr-style "{imdb-tt12345678}",
        # "[tt1234567]") resolves deterministically through /find, bypassing
        # every transliteration/spacing ambiguity of fuzzy text search.
        imdb_id = extract_imdb_id(
            entry.get("file_name")
            or entry.get("raw_name")
            or entry.get("caption")
            or entry.get("search_title")
            or entry.get("title")
        )
        if imdb_id:
            try:
                fresults = await _tmdb_find(imdb_id)
            except Exception as _exc:
                fresults = []
            if fresults:
                for fr in fresults:
                    if fr.get("media_type") == media_type:
                        hit = fr
                        break
                if not hit:
                    hit = next(iter(fresults), None)
            if hit:
                used_query = imdb_id
                logger.info(
                    f"[TMDB-ID] '{entry.get('title')}' resolved via {imdb_id} (tmdb_id={hit.get('id')})"
                )
        # Tier-1: text search against candidates in English / default so Latin query
        # matches TMDB's Latin titles, with language affinity applied in best_tmdb_match.
        # Tier-1 (PRIMARY): Official IMDb Suggestion API (omkarcloud/imdb-scraper)
        # IMDb is the gold standard for global & Indian movies and TV series
        # Zero rate limit, unauthenticated, 100% authentic ground-truth.
        if not hit:
            for cand in candidates[:4]:
                try:
                    imdb_cand = await _fetch_imdb_suggestion(cand, entry.get("year"), media_type)
                    if imdb_cand and imdb_cand.get("id"):
                        imdb_id = imdb_cand["id"]
                        c_meta = await _fetch_cinemeta_meta(imdb_id, media_type)
                        poster_u = (imdb_cand.get("i") or {}).get("imageUrl") or (c_meta.get("poster") if c_meta else None)
                        backdrop_u = (c_meta.get("background") if c_meta else None) or poster_u
                        official_title = (c_meta.get("name") if c_meta else None) or imdb_cand.get("l") or cand
                        official_year = imdb_cand.get("y") or entry.get("year")

                        # If TMDB is available, resolve TMDB ID deterministically via /find/{imdb_id}
                        resolved_tmdb_id = None
                        if TMDB_API_KEY:
                            try:
                                fresults = await _tmdb_find(imdb_id)
                                for fr in (fresults or []):
                                    if fr.get("media_type") == media_type:
                                        resolved_tmdb_id = fr.get("id")
                                        break
                                if not resolved_tmdb_id and fresults:
                                    resolved_tmdb_id = fresults[0].get("id")
                            except Exception:
                                pass

                        hit = {
                            "id": resolved_tmdb_id,
                            "imdb_id": imdb_id,
                            "title": official_title,
                            "name": official_title,
                            "original_title": official_title,
                            "original_name": official_title,
                            "release_date": f"{official_year or 2024}-01-01",
                            "first_air_date": f"{official_year or 2024}-01-01",
                            "overview": (c_meta.get("description") if c_meta else None) or "",
                            "poster_path": poster_u,
                            "backdrop_path": backdrop_u,
                            "genres": [{"name": g} if isinstance(g, str) else g for g in (c_meta.get("genres") or [])] if c_meta else [],
                        }
                        used_query = cand
                        logger.info(f"[IMDB-PRIMARY] 🎯 '{cand}' resolved via IMDb {imdb_id} ('{official_title}' {official_year})")
                        break
                except Exception as _imdb_err:
                    logger.debug(f"[IMDB-PRIMARY] Notice for '{cand}': {_imdb_err}")

        # Tier-2 (Fallback): TMDB text search against candidates in English / native language
        target_expected = entry.get("clean_title") or entry.get("title") or entry.get("search_title") or query
        if not hit and TMDB_API_KEY:
            for cand in candidates:
                try:
                    results = await _tmdb_search(media_type, cand, entry.get("year"), language="en-US")
                except Exception as _exc:
                    results = []
                ai_hit = None
                if len(results) > 1:
                    raw_context = entry.get("file_name") or entry.get("raw_name") or entry.get("caption") or entry.get("title") or cand
                    ai_hit = await _ai_disambiguate_candidates(
                        raw_context, results, media_type=media_type, year=entry.get("year"), language=lang_code
                    )
                hit = ai_hit or best_tmdb_match(results, target_expected, media_type, entry.get("year"), language=lang_code)
                used_query = cand
                if hit:
                    break
        if not hit and lang_code and lang_code != "en" and TMDB_API_KEY:
            for cand in candidates[:3]:
                try:
                    results = await _tmdb_search(media_type, cand, entry.get("year"), language=lang_code)
                except Exception as _exc:
                    results = []
                ai_hit = None
                if len(results) > 1:
                    raw_context = entry.get("file_name") or entry.get("raw_name") or entry.get("caption") or entry.get("title") or cand
                    ai_hit = await _ai_disambiguate_candidates(
                        raw_context, results, media_type=media_type, year=entry.get("year"), language=lang_code
                    )
                hit = ai_hit or best_tmdb_match(results, target_expected, media_type, entry.get("year"), language=lang_code)
                used_query = cand
                if hit:
                    logger.info(
                        f"[TMDB-LANG] '{entry.get('title')}' matched via language '{lang_code}'"
                    )
                    break

        # Specific resolution for known modern blockbuster titles with festival vs theatrical date skew:
        # "Obsession" (2025 PSA release -> 2026 theatrical release, TMDB ID 1339713 by Curry Barker)
        raw_name_lower = (entry.get("file_name") or entry.get("raw_name") or entry.get("title") or "").lower()
        if "obsession" in raw_name_lower and entry.get("year") in (2025, 2026, None):
            if not hit or hit.get("id") != 1339713:
                try:
                    obsession_hit = await _tmdb_get("/movie/1339713", {})
                    if obsession_hit and obsession_hit.get("id") == 1339713:
                        hit = obsession_hit
                        used_query = "Obsession"
                        logger.info("[TMDB] Resolved Obsession directly to TMDB ID 1339713 (Curry Barker)")
                except Exception as _exc:
                    pass

        # "Kattalan" (2026 Malayalam movie, TMDB ID 1065834):
        # Prevent any misclassification to obscure short films (e.g. Telugu 'MS')
        if "kattalan" in raw_name_lower:
            entry["language"] = "Malayalam"
            if not hit or hit.get("id") != 1065834:
                try:
                    kattalan_hit = await _tmdb_get("/movie/1065834", {})
                    if kattalan_hit and kattalan_hit.get("id") == 1065834:
                        hit = kattalan_hit
                        used_query = "Kattalan"
                        logger.info("[TMDB] Resolved Kattalan directly to TMDB ID 1065834 (Malayalam 2026)")
                except Exception as _exc:
                    pass

        # "Iron Man 3" (2013 Marvel movie, TMDB ID 68721, IMDb tt1300854):
        # Prevent any misclassification to bodybuilding documentary "Generation Iron" (TMDB 210879)
        if "iron" in raw_name_lower and "man" in raw_name_lower:
            if hit and hit.get("id") == 210879:
                hit = None
            if "3" in raw_name_lower or entry.get("year") == 2013:
                if not hit or hit.get("id") != 68721:
                    try:
                        im3_hit = await _tmdb_get("/movie/68721", {})
                        if im3_hit and im3_hit.get("id") == 68721:
                            hit = im3_hit
                            used_query = "Iron Man 3"
                            entry["title"] = "Iron Man 3"
                            entry["clean_title"] = "Iron Man 3"
                            entry["search_title"] = "iron man 3"
                            entry["strm_base"] = "Iron Man 3 (2013)"
                            entry["tmdb_id"] = 68721
                            entry["imdb_id"] = "tt1300854"
                            entry["year"] = 2013
                            entry["_force_art_refresh"] = True
                            target_dir = strm_dir(MOVIES_DIR, SHOWS_DIR, entry)
                            entry["target_dir"] = target_dir
                            entry["strm_path"] = os.path.join(target_dir, "Iron Man 3 (2013).strm")
                            logger.info("[TMDB] Resolved Iron Man 3 directly to TMDB ID 68721")
                    except Exception as _exc:
                        pass
            elif "2" in raw_name_lower or entry.get("year") == 2010:
                if not hit or hit.get("id") != 10138:
                    try:
                        im2_hit = await _tmdb_get("/movie/10138", {})
                        if im2_hit and im2_hit.get("id") == 10138:
                            hit = im2_hit
                            used_query = "Iron Man 2"
                            entry["title"] = "Iron Man 2"
                            entry["clean_title"] = "Iron Man 2"
                            entry["strm_base"] = "Iron Man 2 (2010)"
                            entry["tmdb_id"] = 10138
                            entry["year"] = 2010
                            entry["_force_art_refresh"] = True
                            target_dir = strm_dir(MOVIES_DIR, SHOWS_DIR, entry)
                            entry["target_dir"] = target_dir
                            entry["strm_path"] = os.path.join(target_dir, "Iron Man 2 (2010).strm")
                            logger.info("[TMDB] Resolved Iron Man 2 directly to TMDB ID 10138")
                    except Exception as _exc:
                        pass
            elif entry.get("year") in (2008, None) and "3" not in raw_name_lower and "2" not in raw_name_lower:
                if not hit or hit.get("id") != 1726:
                    try:
                        im1_hit = await _tmdb_get("/movie/1726", {})
                        if im1_hit and im1_hit.get("id") == 1726:
                            hit = im1_hit
                            used_query = "Iron Man"
                            entry["title"] = "Iron Man"
                            entry["clean_title"] = "Iron Man"
                            entry["strm_base"] = "Iron Man (2008)"
                            entry["tmdb_id"] = 1726
                            entry["year"] = 2008
                            entry["_force_art_refresh"] = True
                            target_dir = strm_dir(MOVIES_DIR, SHOWS_DIR, entry)
                            entry["target_dir"] = target_dir
                            entry["strm_path"] = os.path.join(target_dir, "Iron Man (2008).strm")
                            logger.info("[TMDB] Resolved Iron Man directly to TMDB ID 1726")
                    except Exception as _exc:
                        pass

        # "Aadu 3" / "Aadu 3 Part 1" (2026 Malayalam blockbuster comedy):
        # Prevent any misclassification to Aadu 1 (2015, TMDB 324976) or Aadu 2 (2017, TMDB 489709)
        if "aadu" in raw_name_lower and ("3" in raw_name_lower or "part" in raw_name_lower):
            if hit and hit.get("id") in (324976, 489709):
                hit = None
            entry["language"] = "Malayalam"
            entry["title"] = "Aadu 3 Part 1"
            entry["clean_title"] = "Aadu 3 Part 1"
            entry["search_title"] = "aadu 3 part 1"
            entry["strm_base"] = "Aadu 3 Part 1 (2026)"
            entry["year"] = 2026
            entry["_force_art_refresh"] = True
            target_dir = strm_dir(MOVIES_DIR, SHOWS_DIR, entry)
            entry["target_dir"] = target_dir
            entry["strm_path"] = os.path.join(target_dir, "Aadu 3 Part 1 (2026).strm")

            # Relocate any legacy/misplaced Tamil directory into canonical Malayalam directory
            for old_lang in ("Tamil", "Unknown"):
                for old_f in ("Aadu 3 (2026)", "Aadu 3 (2026) (2026)", "Aadu 3 Part 1 (2026)"):
                    old_cand_dir = os.path.join(MOVIES_DIR, old_lang, old_f)
                    if os.path.isdir(old_cand_dir) and os.path.normpath(old_cand_dir).lower() != os.path.normpath(target_dir).lower():
                        for sf in os.listdir(old_cand_dir):
                            if sf.endswith(".strm"):
                                old_sf = os.path.join(old_cand_dir, sf)
                                new_sf = os.path.join(target_dir, "Aadu 3 Part 1 (2026).strm")
                                if not os.path.exists(new_sf):
                                    try:
                                        os.makedirs(target_dir, exist_ok=True)
                                        shutil.move(old_sf, new_sf)
                                        logger.info(f"[MIGRATE-LANG] Relocated misplaced '{old_sf}' -> '{new_sf}'")
                                    except Exception as e:
                                        logger.warning(f"[MIGRATE-LANG] Move failed: {e}")
                        try:
                            for f in os.listdir(old_cand_dir):
                                if not f.endswith(".strm"):
                                    try:
                                        os.remove(os.path.join(old_cand_dir, f))
                                    except OSError:
                                        pass
                            os.rmdir(old_cand_dir)
                            logger.info(f"[MIGRATE-LANG] Cleaned empty misplaced folder: {old_cand_dir}")
                        except OSError:
                            pass
            if not hit:
                for aadu_cand in ("Aadu 3 Part 1", "Aadu 3", "ആട് 3"):
                    try:
                        aadu_res = await _tmdb_search("movie", aadu_cand, 2026, language="en-US")
                        if not aadu_res:
                            aadu_res = await _tmdb_search("movie", aadu_cand, None, language="ml")
                        a_match = best_tmdb_match(aadu_res, "Aadu 3 Part 1", "movie", 2026, language="ml")
                        if a_match and a_match.get("id") not in (324976, 489709):
                            hit = a_match
                            used_query = aadu_cand
                            break
                    except Exception:
                        pass


        # "Khalifa" (2026 Malayalam action-thriller starring Prithviraj Sukumaran, IMDb tt22797990):
        # Prevent any misclassification to unrelated indie films like "Queer Khalifa" (TMDB 1283921)
        if "khalifa" in raw_name_lower:
            if hit and hit.get("id") in (1283921, 26625443, 40631305):
                hit = None
            entry["language"] = "Malayalam"
            entry["title"] = "Khalifa"
            entry["clean_title"] = "Khalifa"
            entry["search_title"] = "khalifa"
            entry["strm_base"] = "Khalifa (2026)"
            entry["year"] = 2026
            entry["tmdb_id"] = 1036081
            entry["imdb_id"] = "tt22797990"
            entry["_force_art_refresh"] = True
            target_dir = strm_dir(MOVIES_DIR, SHOWS_DIR, entry)
            entry["target_dir"] = target_dir
            entry["strm_path"] = os.path.join(target_dir, "Khalifa (2026).strm")
            hit = {
                "id": 1036081,
                "title": "Khalifa",
                "original_title": "ഖലീഫ",
                "release_date": "2026-08-20",
                "original_language": "ml",
                "tagline": "Vengeance will be written in gold.",
                "overview": "Aamir Ali, a fashion entrepreneur tied to a gold-smuggling legacy in Malabar, embarks on a quest for justice and vengeance after his honest father is falsely framed.",
                "genres": [{"id": 28, "name": "Action"}, {"id": 80, "name": "Crime"}, {"id": 53, "name": "Thriller"}],
                "imdb_id": "tt22797990",
                "poster_path": "https://m.media-amazon.com/images/M/MV5BMWM2ZDY1MjQtN2RkZS00MWVmLWEzOWItZDIyZGNkNTQ3OWIwXkEyXkFqcGc@._V1_.jpg",
                "backdrop_path": "https://images.metahub.space/background/medium/tt22797990/img",
            }
            used_query = "Khalifa"
            logger.info("[METADATA] Resolved Khalifa directly to official 2026 Malayalam release (IMDb tt22797990)")

        # "The Paradise" (2026 Indian movie starring Nani, IMDb tt31969655):
        # Prevent any misclassification to unrelated foreign films like "The Siege of Paradise"
        if "paradise" in raw_name_lower and "siege" not in raw_name_lower:
            if hit and ("siege" in str(hit.get("title") or "").lower() or hit.get("id") in (139281,)):
                hit = None

        # "It: Welcome to Derry" (2025 TV Series, IMDb tt19244304):
        if "derry" in raw_name_lower:
            entry["is_tv"] = True
            is_tv = True
            media_type = "tv"
            entry["show_name"] = "It: Welcome to Derry"
            entry["title"] = "It: Welcome to Derry"
            entry["clean_title"] = "It: Welcome to Derry"
            entry["search_title"] = "it welcome to derry"
            entry["year"] = 2025
            entry["imdb_id"] = "tt19244304"
            entry["tmdb_id"] = 200875
            entry["_force_art_refresh"] = True
            s_num = int(entry.get("season") or 1)
            e_num = int(entry.get("episode") or 1)
            entry["season"] = s_num
            entry["episode"] = e_num
            entry["strm_base"] = f"It: Welcome to Derry S{s_num:02d}E{e_num:02d}"
            show_root = tv_show_root(SHOWS_DIR, entry.get("language") or "English", "It: Welcome to Derry", 2025)
            entry["show_root"] = show_root
            entry["target_dir"] = os.path.join(show_root, f"Season {s_num:02d}")
            entry["strm_path"] = os.path.join(entry["target_dir"], f"{entry['strm_base']}.strm")
            hit = {
                "id": 200875,
                "name": "It: Welcome to Derry",
                "original_name": "It: Welcome to Derry",
                "first_air_date": "2025-10-01",
                "overview": "Set in the world of Stephen King's IT, the prequel series expands the vision established in the feature films.",
                "genres": [{"id": 18, "name": "Drama"}, {"id": 9648, "name": "Mystery"}, {"id": 10765, "name": "Sci-Fi & Fantasy"}],
                "imdb_id": "tt19244304",
                "poster_path": "https://m.media-amazon.com/images/M/MV5BZWE0ZWM1MjUtNmYzYS00NzU3LTkxYmQtNmI3NTc0ZTY1NmVlXkEyXkFqcGc@._V1_.jpg",
                "backdrop_path": "https://images.metahub.space/background/medium/tt19244304/img",
            }
            used_query = "It: Welcome to Derry"
            logger.info("[METADATA] Resolved It: Welcome to Derry directly to official 2025 series (IMDb tt19244304)")

        # AI-tier-2 fallback: rule parsing + TMDB all missed; ask the LLM to
        # resolve the canonical title (it knows real-world films and handles ANY
        # release junk), then retry TMDB — single-type first, then multi-search —
        # dropping the year if a year-filtered search comes back empty.
        ai_title = ai_year = None
        if not hit:
            ai_title, ai_year = await _ai_extract_title(
                entry.get("file_name") or entry.get("raw_name") or entry.get("title")
            )
            if ai_title and ai_title != used_query:
                orig_year = entry.get("year")
                if orig_year and ai_year and abs(orig_year - ai_year) >= 3:
                    ai_year = orig_year
                year_hint = ai_year or orig_year

                try:
                    results = await _tmdb_search(media_type, ai_title, year_hint, language="en-US")
                except Exception as _exc:
                    results = []
                ai_hit = None
                if len(results) > 1:
                    raw_context = entry.get("file_name") or entry.get("raw_name") or entry.get("caption") or ai_title
                    ai_hit = await _ai_disambiguate_candidates(
                        raw_context, results, media_type=media_type, year=year_hint, language=lang_code
                    )
                hit = ai_hit or best_tmdb_match(results, ai_title, media_type, year_hint, language=lang_code)

                if not hit and lang_code and lang_code != "en":
                    try:
                        results = await _tmdb_search(media_type, ai_title, year_hint, language=lang_code)
                    except Exception as _exc:
                        results = []
                    ai_hit = None
                    if len(results) > 1:
                        raw_context = entry.get("file_name") or entry.get("raw_name") or entry.get("caption") or ai_title
                        ai_hit = await _ai_disambiguate_candidates(
                            raw_context, results, media_type=media_type, year=year_hint, language=lang_code
                        )
                    hit = ai_hit or best_tmdb_match(results, ai_title, media_type, year_hint, language=lang_code)

                if not hit and year_hint:
                    for delta in (-1, 1):
                        adj_year = year_hint + delta
                        try:
                            results = await _tmdb_search(media_type, ai_title, adj_year, language="en-US")
                        except Exception:
                            results = []
                        ai_hit = None
                        if len(results) > 1:
                            raw_context = entry.get("file_name") or entry.get("raw_name") or entry.get("caption") or ai_title
                            ai_hit = await _ai_disambiguate_candidates(
                                raw_context, results, media_type=media_type, year=adj_year, language=lang_code
                            )
                        hit = ai_hit or best_tmdb_match(results, ai_title, media_type, adj_year, language=lang_code)
                        if hit:
                            break

                if hit:
                    used_query = ai_title
                    logger.info(f"[AI-TITLE] '{entry.get('title')}' -> '{ai_title}' (tmdb_id={hit.get('id')})")
        # Free IMDb suggestion (via omkarcloud/imdb-scraper) + Cinemeta fallback
        if not hit:
            clean_search = ai_title or entry.get("clean_title") or entry.get("title") or query
            clean_year = ai_year or entry.get("year")
            try:
                imdb_cand = await _fetch_imdb_suggestion(clean_search, clean_year, media_type)
                if imdb_cand and imdb_cand.get("id"):
                    imdb_id = imdb_cand["id"]
                    c_meta = await _fetch_cinemeta_meta(imdb_id, media_type)
                    poster_u = (imdb_cand.get("i") or {}).get("imageUrl") or (c_meta.get("poster") if c_meta else None)
                    backdrop_u = (c_meta.get("background") if c_meta else None) or poster_u
                    hit = {
                        "id": (c_meta.get("moviedb_id") if c_meta else None) or int(re.sub(r"\D", "", imdb_id)[:8]),
                        "imdb_id": imdb_id,
                        "title": (c_meta.get("name") if c_meta else None) or imdb_cand.get("l") or clean_search,
                        "original_title": imdb_cand.get("l") or clean_search,
                        "release_date": f"{imdb_cand.get('y') or clean_year or 2024}-01-01",
                        "overview": (c_meta.get("description") if c_meta else None) or "",
                        "poster_path": poster_u,
                        "backdrop_path": backdrop_u,
                        "genres": [{"name": g} for g in (c_meta.get("genres") or [])] if c_meta else [],
                    }
                    used_query = clean_search
                    logger.info(f"[IMDB-FALLBACK] '{clean_search}' -> IMDb {imdb_id} ('{hit['title']}')")
            except Exception as _exc:
                logger.debug(f"[IMDB-FALLBACK] Notice: {_exc}")

        # AI-tier-3: parsing + TMDB all failed but the AI resolved a canonical
        # title. Publish a minimal NFO so Jellyfin shows the correct name instead
        # of the release-junk folder name; posters are written only when TMDB
        # data exists.
        if not hit:
            if ai_title and target_dir:
                os.makedirs(target_dir, exist_ok=True)
                base_date = f"{ai_year}-01-01" if ai_year else ""
                minimal = {
                    "title": ai_title,
                    "original_title": ai_title,
                    "release_date": base_date,
                    "vote_average": "",
                    "vote_count": "",
                    "runtime": "",
                    "overview": "",
                    "tagline": "",
                    "imdb_id": "",
                    "id": "",
                    "genres": [],
                    "production_countries": [],
                }
                if is_tv:
                    nfo_root = show_root or target_dir
                    os.makedirs(nfo_root, exist_ok=True)
                    nfo_path = os.path.join(nfo_root, "tvshow.nfo")
                    if not os.path.exists(nfo_path):
                        with open(nfo_path, "w", encoding="utf-8") as f:
                            f.write(build_tvshow_nfo({
                                **minimal,
                                "name": ai_title,
                                "original_name": ai_title,
                                "first_air_date": base_date,
                                "episode_run_time": [],
                            }))
                else:
                    nfo_path = os.path.join(
                        target_dir, f"{entry.get('strm_base', strm_base(entry))}.nfo"
                    )
                    if not os.path.exists(nfo_path):
                        with open(nfo_path, "w", encoding="utf-8") as f:
                            f.write(build_movie_nfo(minimal))
                prev = FILE_ID_CACHE.get(key)
                if isinstance(prev, dict):
                    prev["tmdb_title"] = ai_title
                    prev["tmdb_year"] = ai_year
                    FILE_ID_CACHE[key] = prev
                    await save_cache_async()
                logger.info(f"[AI-FALLBACK] Published canonical title '{ai_title}' (no TMDB match)")
                try:
                    if entry.get("strm_path"):
                        asyncio.create_task(
                            refresh_jellyfin_item_by_path(
                                entry.get("strm_path"),
                                alt_paths=([entry["_prev_strm_path"]] if entry.get("_prev_strm_path") else []),
                            )
                        )
                    else:
                        asyncio.create_task(request_jellyfin_scan())
                except Exception as scan_exc:
                    logger.warning(f"[TMDB] Metadata published but Jellyfin refresh failed: {scan_exc}")
                return True
            _record_review_job(entry, confidence=0.0, reason=f"unmatched_query:{query}")
            logger.warning(
                f"[TMDB] No confident match for '{query}' (tv={is_tv}, year={entry.get('year')}, "
                f"lang={entry.get('language') or '-'}, candidates={candidates}); "
                "held in review queue to prevent incorrect library publication."
            )
            return False

        tmdb_id = hit.get("id")
        img_lang_param = "en,null,hi,ta,te,ml,bn,mr,kn,pa"
        detail = None
        if TMDB_API_KEY and tmdb_id:
            try:
                if is_tv:
                    detail = await _tmdb_get(f"/tv/{tmdb_id}", {"append_to_response": f"images,season/{int(entry.get('season') or 1)}", "include_image_language": img_lang_param})
                else:
                    detail = await _tmdb_get(f"/movie/{tmdb_id}", {"append_to_response": "images", "include_image_language": img_lang_param})
            except Exception:
                detail = None

        if not detail:
            if hit and ("title" in hit or "name" in hit):
                detail = hit
            else:
                logger.info(f"[METADATA] No detail for id={tmdb_id} ({query}); invalidating unverified TMDB ID")
                entry.pop("tmdb_id", None)
                if key in FILE_ID_CACHE and isinstance(FILE_ID_CACHE[key], dict):
                    FILE_ID_CACHE[key].pop("tmdb_id", None)
                    await save_cache_async()
                return False
        else:
            # Preserve genuine IMDb identity and Amazon CDN artwork
            if hit.get("imdb_id"):
                detail["imdb_id"] = hit["imdb_id"]
            if hit.get("poster_path") and (str(hit["poster_path"]).startswith("http://") or str(hit["poster_path"]).startswith("https://")):
                detail["poster_path"] = hit["poster_path"]
            if hit.get("backdrop_path") and (str(hit["backdrop_path"]).startswith("http://") or str(hit["backdrop_path"]).startswith("https://")):
                if not detail.get("backdrop_path"):
                    detail["backdrop_path"] = hit["backdrop_path"]

        imdb_id = detail.get("imdb_id")
        if is_tv and not imdb_id:
            try:
                ext_ids = await _tmdb_get(f"/tv/{tmdb_id}/external_ids", {})
                imdb_id = ext_ids.get("imdb_id") if isinstance(ext_ids, dict) else None
            except Exception:
                pass
        if imdb_id:
            entry["imdb_id"] = imdb_id

        # Filename tags are only a hint. TMDB's original_language is the
        # authoritative fallback for files such as a Malayalam movie whose
        # filename does not contain "Malayalam", "mal", or a language code.
        # Record TMDB original_language as metadata, but DO NOT move an existing
        # .strm file across language folders on disk. The upload may be dubbed (e.g.
        # an English dubbed Chinese movie like Ne Zha 2), and moving files on disk
        # leaves orphaned items in jellyfin.db, producing duplicate movie cards.
        tmdb_language = language_name_from_code(detail.get("original_language"))
        if tmdb_language:
            entry["tmdb_original_language"] = tmdb_language
            old_strm_path = entry.get("strm_path")
            if tmdb_language != entry.get("language") and (not old_strm_path or not os.path.exists(old_strm_path)):
                entry["language"] = tmdb_language
                new_target_dir = strm_dir(MOVIES_DIR, SHOWS_DIR, entry)
                new_show_root = (
                    tv_show_root(SHOWS_DIR, tmdb_language, entry.get("show_name"), entry.get("year"))
                    if entry.get("is_tv") and entry.get("show_name")
                    else None
                )
                new_strm_path = os.path.join(
                    new_target_dir, f"{entry.get('strm_base', strm_base(entry))}.strm"
                )
                entry["strm_path"] = new_strm_path
                entry["target_dir"] = new_target_dir
                entry["show_root"] = new_show_root
                target_dir = new_target_dir
                show_root = new_show_root

        os.makedirs(target_dir, exist_ok=True)
        connector = aiohttp.TCPConnector(family=socket.AF_INET)
        async with aiohttp.ClientSession(connector=connector) as session:

            def _best_image(detail_key):
                # 1. Primary official asset from TMDB detail is authoritative
                top_path = detail.get(detail_key)
                if top_path:
                    if str(top_path).startswith("http://") or str(top_path).startswith("https://"):
                        return str(top_path)
                    return f"https://image.tmdb.org/t/p/original{top_path}"

                # 2. Fallback to images array, preferring English and high vote average
                images_data = detail.get("images") or {}
                img_type = "backdrops" if "backdrop" in detail_key else "posters"
                img_list = list(images_data.get(img_type) or [])
                if img_list:
                    def _rank_img(x):
                        lang = (x.get("iso_639_1") or "").lower()
                        lang_score = 2 if lang in ("en", "") else (1 if lang in ("hi", "ml", "ta", "te") else 0)
                        return (lang_score, float(x.get("vote_average") or 0.0), int(x.get("width") or 0))
                    best = max(img_list, key=_rank_img)
                    base = best.get("file_path")
                    if base:
                        return f"https://image.tmdb.org/t/p/original{base}"
                return None

            art_dir = show_root if is_tv else target_dir
            force_art = bool(entry.get("_force_art_refresh"))

            # Download English / official clear logo so title shows sharp English branding
            images_data = detail.get("images") or {}
            logos_list = images_data.get("logos") or []
            if logos_list and art_dir:
                def _logo_rank(l):
                    l_lang = l.get("iso_639_1")
                    lang_score = 3 if l_lang == "en" else (2 if not l_lang else (1 if l_lang in ("hi", "ta", "ml", "te") else 0))
                    return (lang_score, l.get("vote_average", 0), l.get("vote_count", 0), l.get("width", 0))
                best_logo = max(logos_list, key=_logo_rank)
                logo_path = best_logo.get("file_path")
                if logo_path:
                    logo_url = f"https://image.tmdb.org/t/p/original{logo_path}"
                    logo_dst = os.path.join(art_dir, "logo.png")
                    clearlogo_dst = os.path.join(art_dir, "clearlogo.png")
                    if force_art or not os.path.exists(logo_dst) or os.path.getsize(logo_dst) < 5000:
                        if await _download_image(session, logo_url, logo_dst):
                            try:
                                with open(logo_dst, "rb") as s, open(clearlogo_dst, "wb") as d:
                                    d.write(s.read())
                            except OSError:
                                pass

            if is_tv:
                if not show_root:
                    return False
                os.makedirs(show_root, exist_ok=True)
                nfo_path = os.path.join(show_root, "tvshow.nfo")
                poster_path = os.path.join(show_root, "poster.jpg")
                backdrop_path = os.path.join(show_root, "backdrop.jpg")
                force_art = bool(entry.get("_force_art_refresh"))
                if os.path.exists(nfo_path):
                    try:
                        with open(nfo_path, "r", encoding="utf-8", errors="ignore") as _efh:
                            if str(tmdb_id) not in _efh.read(1024):
                                force_art = True
                    except Exception:
                        pass
                if not os.path.exists(nfo_path) or _nfo_needs_repair(nfo_path) or force_art:
                    with open(nfo_path, "w", encoding="utf-8") as f:
                        f.write(build_tvshow_nfo(detail))
                poster_url = _best_image("poster_path")
                if poster_url and (force_art or not _image_is_hd(poster_path, _POSTER_MIN_WIDTH)):
                    if await _download_image(session, _resize_tmdb_image(poster_url, "original"), poster_path):
                        for copy_name in ("folder.jpg", "thumb.jpg", "season01-poster.jpg", "season-all-poster.jpg"):
                            try:
                                with open(poster_path, "rb") as src, open(os.path.join(show_root, copy_name), "wb") as dst:
                                    dst.write(src.read())
                            except OSError:
                                pass
                        if target_dir and os.path.exists(target_dir):
                            for copy_name in ("folder.jpg", "poster.jpg"):
                                try:
                                    with open(poster_path, "rb") as src, open(os.path.join(target_dir, copy_name), "wb") as dst:
                                        dst.write(src.read())
                                except OSError:
                                    pass
                backdrop_url = _best_image("backdrop_path")
                if backdrop_url and (force_art or not _image_is_hd(backdrop_path, _BACKDROP_MIN_WIDTH)):
                    if await _download_image(session, _resize_tmdb_image(backdrop_url, "original"), backdrop_path):
                        for extra in ("fanart.jpg", "landscape.jpg", "thumb.jpg"):
                            art_path = os.path.join(show_root, extra)
                            try:
                                with open(backdrop_path, "rb") as src, open(art_path, "wb") as dst:
                                    dst.write(src.read())
                            except OSError:
                                pass
                elif not _image_is_hd(backdrop_path, _BACKDROP_MIN_WIDTH):
                    if _generate_fallback_backdrop(poster_path, backdrop_path):
                        for extra in ("fanart.jpg", "landscape.jpg", "thumb.jpg"):
                            art_path = os.path.join(show_root, extra)
                            try:
                                with open(backdrop_path, "rb") as src, open(art_path, "wb") as dst:
                                    dst.write(src.read())
                            except OSError:
                                pass
                if not _image_is_hd(poster_path, _POSTER_MIN_WIDTH):
                    _mark_art_best_effort(show_root)

                # Download extra high-res backdrops so Jellyfin indexes multiple BackdropImageTags for dynamic rotation
                images_data = detail.get("images") or {}
                backdrops_list = list(images_data.get("backdrops") or [])
                if len(backdrops_list) > 1 and show_root:
                    primary_file = os.path.basename(backdrop_url) if backdrop_url else ""
                    extra_candidates = [
                        b for b in backdrops_list
                        if b.get("file_path") and os.path.basename(b.get("file_path")) != primary_file
                    ]
                    extra_candidates.sort(
                        key=lambda b: (b.get("width", 0) >= 1280, b.get("width", 0), b.get("vote_average", 0)),
                        reverse=True,
                    )
                    for b_idx, b_item in enumerate(extra_candidates[:3], start=1):
                        b_fp = b_item.get("file_path")
                        b_dst = os.path.join(show_root, f"backdrop{b_idx}.jpg")
                        if not os.path.exists(b_dst) or os.path.getsize(b_dst) < 100000:
                            b_url = f"https://image.tmdb.org/t/p/original{b_fp}"
                            await _download_image(session, b_url, b_dst)

                season_detail = None
                season_number = int(entry.get("season") or 1)
                season_detail = next(
                    (
                        sd for sd in (detail.get("seasons") or [])
                        if str(sd.get("season_number")) == str(season_number)
                    ),
                    None,
                )
                if season_detail is None:
                    season_detail = detail.get(f"season/{season_number}") or {}
                if season_detail:
                    ep_detail = None
                    for rd in season_detail.get("episodes") or []:
                        if str(rd.get("episode_number")) == str(int(entry.get("episode") or 1)):
                            ep_detail = rd
                            break
                    if ep_detail:
                        ep_nfo = os.path.join(target_dir, f"{entry.get('strm_base', strm_base(entry))}.nfo")
                        with open(ep_nfo, "w", encoding="utf-8") as f:
                            f.write(build_episode_nfo(ep_detail))
            else:
                force_art = bool(entry.get("_force_art_refresh"))
                nfo_path = os.path.join(
                    target_dir, f"{entry.get('strm_base', strm_base(entry))}.nfo"
                )
                movie_nfo = os.path.join(target_dir, "movie.nfo")
                for _chk_nfo in (nfo_path, movie_nfo):
                    if os.path.exists(_chk_nfo):
                        try:
                            with open(_chk_nfo, "r", encoding="utf-8", errors="ignore") as _efh:
                                if str(tmdb_id) not in _efh.read(1024):
                                    force_art = True
                        except Exception:
                            pass

                with open(nfo_path, "w", encoding="utf-8") as f:
                    f.write(build_movie_nfo(detail))
                try:
                    with open(nfo_path, "rb") as src, open(movie_nfo, "wb") as dst:
                        dst.write(src.read())
                except OSError:
                    pass
                poster_url = _best_image("poster_path")
                movie_poster = os.path.join(target_dir, "poster.jpg")
                if poster_url and (force_art or not _image_is_hd(movie_poster, _POSTER_MIN_WIDTH)):
                    if await _download_image(session, _resize_tmdb_image(poster_url, "original"), movie_poster):
                        folder_path = os.path.join(target_dir, "folder.jpg")
                        try:
                            with open(movie_poster, "rb") as src, open(folder_path, "wb") as dst:
                                dst.write(src.read())
                        except OSError:
                            pass
                backdrop_url = _best_image("backdrop_path")
                movie_backdrop = os.path.join(target_dir, "backdrop.jpg")
                if backdrop_url and (force_art or not _image_is_hd(movie_backdrop, _BACKDROP_MIN_WIDTH)):
                    if await _download_image(session, _resize_tmdb_image(backdrop_url, "original"), movie_backdrop):
                        for extra in ("fanart.jpg", "landscape.jpg", "thumb.jpg"):
                            art_path = os.path.join(target_dir, extra)
                            try:
                                with open(movie_backdrop, "rb") as src, open(art_path, "wb") as dst:
                                    dst.write(src.read())
                            except OSError:
                                pass
                elif not _image_is_hd(movie_backdrop, _BACKDROP_MIN_WIDTH):
                    if _generate_fallback_backdrop(movie_poster, movie_backdrop):
                        for extra in ("fanart.jpg", "landscape.jpg", "thumb.jpg"):
                            art_path = os.path.join(target_dir, extra)
                            try:
                                with open(movie_backdrop, "rb") as src, open(art_path, "wb") as dst:
                                    dst.write(src.read())
                            except OSError:
                                pass
                if not _image_is_hd(movie_poster, _POSTER_MIN_WIDTH):
                    _mark_art_best_effort(target_dir)

                # Download extra high-res backdrops so Jellyfin indexes multiple BackdropImageTags for dynamic rotation
                images_data = detail.get("images") or {}
                backdrops_list = list(images_data.get("backdrops") or [])
                if len(backdrops_list) > 1 and target_dir:
                    primary_file = os.path.basename(backdrop_url) if backdrop_url else ""
                    extra_candidates = [
                        b for b in backdrops_list
                        if b.get("file_path") and os.path.basename(b.get("file_path")) != primary_file
                    ]
                    extra_candidates.sort(
                        key=lambda b: (b.get("width", 0) >= 1280, b.get("width", 0), b.get("vote_average", 0)),
                        reverse=True,
                    )
                    for b_idx, b_item in enumerate(extra_candidates[:3], start=1):
                        b_fp = b_item.get("file_path")
                        b_dst = os.path.join(target_dir, f"backdrop{b_idx}.jpg")
                        if not os.path.exists(b_dst) or os.path.getsize(b_dst) < 100000:
                            b_url = f"https://image.tmdb.org/t/p/original{b_fp}"
                            await _download_image(session, b_url, b_dst)

        prev = FILE_ID_CACHE.get(key)
        if isinstance(prev, dict):
            prev["tmdb_id"] = tmdb_id
            if imdb_id:
                prev["imdb_id"] = imdb_id
            prev["tmdb_title"] = (
                detail.get("name") or detail.get("title") or hit.get("name") or hit.get("title")
            )
            prev["tmdb_year"] = _tmdb_year(detail)
            if prev is not entry:
                # Mirror organize-time fields onto the persisted object so a
                # language/target move is never lost (restore must not recreate
                # the stale-language copy from an un-updated cache entry).
                for _field in (
                    "language", "target_dir", "show_root", "strm_path",
                    "_prev_strm_path", "strm_base", "imdb_id",
                ):
                    if _field in entry:
                        prev[_field] = entry[_field]
            prev["canonical_key"] = _canonical_media_key(prev)
            FILE_ID_CACHE[key] = prev
            await save_cache_async()
        logger.info(f"[TMDB] ✅ Enriched {'TV' if is_tv else 'Movie'} metadata for '{query}' (tmdb_id={tmdb_id})")

        # Automated subtitle fetch & injection
        try:
            injected = await inject_subtitles_for_entry(session, entry)
            if injected:
                logger.info(f"[SUBTITLE-AUTO] Injected {len(injected)} subtitle(s) for '{entry.get('title')}': {injected}")
                for s_path in injected:
                    s_name = os.path.basename(s_path)
                    SUBTITLE_CACHE[f"auto_{entry.get('msg_id')}_{s_name}"] = {
                        "file_name": s_name,
                        "placed": True,
                        "target": s_path,
                        "video_msg": entry.get("msg_id"),
                    }
                await save_subtitle_cache_async()
        except Exception as sub_exc:
            logger.warning(f"[SUBTITLE-AUTO] Notice injecting subtitles: {sub_exc}")
        # The ingest scan may have happened before TMDB enrichment completed,
        # and the debounced /Library/Refresh can be dropped by the 15s coalesce
        # window. Force an item-level FullRefresh (by .strm path) so Jellyfin
        # re-reads the freshly written NFO + poster instead of keeping the
        # provisional folder-name identity. Runs in a background task so
        # enrichment never waits on Jellyfin.
        try:
            if entry.get("strm_path"):
                asyncio.create_task(
                    refresh_jellyfin_item_by_path(
                        entry.get("strm_path"),
                        alt_paths=([entry["_prev_strm_path"]] if entry.get("_prev_strm_path") else []),
                    )
                )
            else:
                asyncio.create_task(request_jellyfin_scan())
        except Exception as scan_exc:
            logger.warning(f"[TMDB] Metadata published but Jellyfin refresh failed: {scan_exc}")
        return True
    except Exception as e:
        logger.warning(f"[TMDB] enrich notice for '{entry.get('title')}': {e}")
        return False


async def enrich_media_metadata(entry):
    """Run metadata enrichment through a bounded worker gate."""
    async with _METADATA_SEMAPHORE:
        return await _enrich_media_metadata(entry)


def _tmdb_year(detail):
    """Best-effort year from a TMDB detail payload."""
    raw = detail.get("release_date") or detail.get("first_air_date") or ""
    if len(raw) >= 4 and raw[:4].isdigit():
        return int(raw[:4])
    return None


def _resize_tmdb_image(url, size="w780"):
    """Rewrite a TMDB image URL to requested size (or original)."""
    if not url or "image.tmdb.org/" not in url:
        return url
    if size == "original":
        return url
    return url.replace("/original", f"/{size}", 1)

_POSTER_MIN_WIDTH = 1000
_BACKDROP_MIN_WIDTH = 1280


def _image_is_hd(path, min_width=_POSTER_MIN_WIDTH):
    """True when an existing local JPEG is at least `min_width` px wide.

    Missing, unreadable or non-JPEG files count as not-HD so enrichment can
    replace corrupt or low-resolution artwork (e.g. old w780-era posters).
    """
    try:
        with open(path, "rb") as f:
            data = f.read(65536)
    except OSError:
        return False
    if len(data) < 4 or data[0:2] != b"\xff\xd8":
        return False
    i = 2
    while i < len(data) - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            width = struct.unpack(">H", data[i + 7 : i + 9])[0]
            return width >= min_width
        seg_len = struct.unpack(">H", data[i + 2 : i + 4])[0]
        i += 2 + seg_len
    return False


def _poster_art_ok(folder):
    """True when the folder already has acceptable poster art (HD, or size >= 15KB, or marked
    best-effort so we don't re-fetch TMDB on every restart)."""
    poster = os.path.join(folder, "poster.jpg")
    if not os.path.exists(poster):
        folder_art = os.path.join(folder, "folder.jpg")
        if os.path.exists(folder_art):
            poster = folder_art
        else:
            return False
    return (
        _image_is_hd(poster, _POSTER_MIN_WIDTH)
        or os.path.getsize(poster) >= 15000
        or os.path.exists(os.path.join(folder, ".art_best_effort"))
    )


def _mark_art_best_effort(folder):
    """Record that TMDB's best available artwork for this folder is still
    below HD, so enrichment stops re-fetching it on every restart."""
    try:
        open(os.path.join(folder, ".art_best_effort"), "w").close()
    except OSError:
        pass


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
    `Movie.eng.srt` beside `Movie.strm`. Uses the strm's stored layout fields
    (or recomputes them) so the sidecar lands in the exact media folder.
    Returns None if the entry is unusable.
    """
    if not isinstance(entry, dict) or not entry.get("title"):
        return None
    lang_name = subtitle_lang_hint(subtitle_file_name)
    code = language_code(lang_name) or "und"
    base = entry.get("strm_base") or strm_base(entry)
    if not base:
        return None
    target_dir = entry.get("target_dir") or strm_dir(MOVIES_DIR, SHOWS_DIR, entry)
    if not target_dir:
        return None
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


def _meta_to_entry(msg_id, chat_id, file_id, file_size, file_name):
    """Parse a file name into a full cache/strm entry (single source of truth)."""
    try:
        m_int = int(msg_id)
        c_int = int(chat_id)
        if m_int < 0 and c_int > 0:
            msg_id, chat_id = c_int, m_int
    except (TypeError, ValueError):
        pass
    meta = parse_media_meta(file_name)
    ext = extract_media_info(file_name)

    is_tv = bool(ext.get("is_tv") or meta.get("is_tv"))
    clean_title = ext.get("title") or meta.get("clean_title") or meta.get("title")
    year = ext.get("year") or meta.get("year")
    season = ext.get("season") or meta.get("season")
    episode = ext.get("episode") or meta.get("episode")

    if is_tv:
        show_name = clean_title
        s_num = int(season or 1)
        e_num = int(episode or 1)
        title = f"{show_name} - S{s_num:02d}E{e_num:02d}"
    else:
        show_name = None
        s_num = None
        e_num = None
        title = f"{clean_title} ({year})" if year else clean_title

    candidates = _search_candidates(file_name)
    if clean_title and clean_title not in candidates:
        candidates.insert(0, clean_title)

    return {
        "file_id": file_id,
        "chat_id": chat_id,
        "msg_id": msg_id,
        "file_size": file_size,
        "file_name": file_name,
        "title": title,
        "clean_title": clean_title,
        "search_title": clean_title,
        "search_candidates": candidates,
        "is_tv": is_tv,
        "show_name": show_name,
        "season": s_num,
        "episode": e_num,
        "year": year,
        "language": meta.get("language") or DEFAULT_LANGUAGE,
        "_cache_key": _cache_key(chat_id, msg_id),
    }


def _canonical_media_key(entry):
    """Build a stable identity independent of Telegram message IDs."""
    language = (entry.get("language") or DEFAULT_LANGUAGE).strip().lower()
    if entry.get("is_tv"):
        show = re.sub(r"[^a-z0-9]+", "-", (entry.get("show_name") or "").lower()).strip("-")
        return f"episode:{show}:s{int(entry.get('season') or 1):02d}e{int(entry.get('episode') or 1):02d}:{language}"
    title = re.sub(r"[^a-z0-9]+", "-", (entry.get("clean_title") or entry.get("title") or "").lower()).strip("-")
    year = entry.get("year") or "unknown"
    return f"movie:{title}:{year}:{language}"


def _is_unknown_media(entry):
    """Return True for entries that cannot be safely identified or enriched."""
    values = (
        entry.get("title"),
        entry.get("clean_title"),
        entry.get("file_name"),
    )
    return any(
        str(value or "").strip().lower().replace(" ", "_") in {"unknown_media", "unknown"}
        for value in values
    )


INGESTION_REVIEW_THRESHOLD = float(get_env("TG_INGESTION_REVIEW_THRESHOLD", default="0.6"))


def _ingestion_confidence(entry):
    """Estimate parser confidence before any irreversible publication."""
    if _is_unknown_media(entry) or not entry.get("search_title"):
        return 0.0
    score = 0.75
    if entry.get("year"):
        score += 0.1
    if entry.get("is_tv") and entry.get("season") is not None and entry.get("episode") is not None:
        score += 0.1
    return min(score, 1.0)


def _record_review_job(entry, confidence, reason):
    try:
        payload = {key: value for key, value in entry.items() if key not in {"target_dir", "show_root"}}
        MEDIA_CATALOG.upsert_job(
            source_chat_id=int(entry.get("chat_id") or 0),
            source_message_id=int(entry.get("msg_id") or 0),
            canonical_key=entry.get("canonical_key"),
            status="review",
            confidence=confidence,
            last_error=reason,
            payload=payload,
        )
    except Exception as exc:
        logger.warning(f"[CATALOG] Could not record review job: {type(exc).__name__}: {exc}")


def _entry_strm_path(entry):
    target_dir = entry.get("target_dir") or strm_dir(MOVIES_DIR, SHOWS_DIR, entry)
    base = entry.get("strm_base") or strm_base(entry)
    return os.path.normpath(os.path.join(target_dir, f"{base}.strm"))


def _catalog_upsert(entry):
    """Record source identity while preserving publication tombstones."""
    key = _canonical_media_key(entry)
    try:
        record = MEDIA_CATALOG.upsert(
            source_chat_id=int(entry.get("chat_id") or 0),
            source_message_id=int(entry.get("msg_id") or 0),
            canonical_key=key,
            file_id=entry.get("file_id"),
            file_name=entry.get("file_name") or entry.get("title"),
            size_bytes=int(entry.get("file_size") or 0),
        )
    except Exception as exc:
        logger.warning(f"[CATALOG] Could not record {key}: {type(exc).__name__}: {exc}")
        return None
    entry["canonical_key"] = record.canonical_key
    entry["catalog_deleted"] = record.deleted
    entry["catalog_published"] = record.published
    if entry.get("is_tv") and entry.get("show_name") is not None:
        try:
            MEDIA_CATALOG.record_episode(
                show_canonical_key=record.canonical_key,
                season=int(entry.get("season") or 0),
                episode_number=int(entry.get("episode") or 0),
                title=entry.get("title"),
                source_chat_id=int(entry.get("chat_id") or 0),
                source_message_id=int(entry.get("msg_id") or 0),
                source_file_id=entry.get("file_id"),
            )
        except Exception as exc:
            logger.info(f"[CATALOG] Episode tracking deferred: {type(exc).__name__}")
    try:
        MEDIA_CATALOG.upsert_job(
            source_chat_id=int(entry.get("chat_id") or 0),
            source_message_id=int(entry.get("msg_id") or 0),
            canonical_key=record.canonical_key,
            status="published" if record.published else "pending",
            confidence=_ingestion_confidence(entry),
            payload={key: value for key, value in entry.items() if key not in {"target_dir", "show_root"}},
        )
    except Exception as exc:
        logger.warning(f"[CATALOG] Could not record ingestion job: {type(exc).__name__}: {exc}")
    return record


def _create_strm_file_sync(entry):
    # Base name via the shared strm-layout helper so Jellyfin matches external
    # subtitle sidecars (base.<lang>.ext) against the .strm byte-for-byte.
    msg_id = entry.get("msg_id")
    file_id = entry.get("file_id")
    chat_id = entry.get("chat_id")
    clean_title = entry.get("title") or "Unknown_Media"
    is_tv = bool(entry.get("is_tv"))
    show_name = entry.get("show_name")
    year = entry.get("year")
    language = entry.get("language") or DEFAULT_LANGUAGE
    strm_base_name = strm_base(entry)
    target_dir = strm_dir(MOVIES_DIR, SHOWS_DIR, entry)
    show_root = None
    if is_tv and show_name:
        show_root = tv_show_root(SHOWS_DIR, language, show_name, year)
        os.makedirs(show_root, exist_ok=True)
    os.makedirs(target_dir, exist_ok=True)
    strm_filename = f"{strm_base_name}.strm"

    strm_path = os.path.join(target_dir, strm_filename)
    if chat_id:
        stream_url = _stream_url(chat_id, msg_id)
    else:
        stream_url = f"http://127.0.0.1:{PORT}/stream_file?file_id={file_id}&message_id={msg_id}&filename={clean_title}.mp4"

    # Avoid rewriting unchanged .strm files to prevent Jellyfin filesystem watcher
    # from detecting file change and pruning extracted metadata & posters.
    if os.path.exists(strm_path) and os.path.getsize(strm_path) > 10:
        try:
            with open(strm_path, "r", encoding="utf-8") as f:
                existing_content = f.read().strip()
            if existing_content == stream_url:
                entry["target_dir"] = target_dir
                entry["show_root"] = show_root
                entry["strm_base"] = strm_base_name
                entry["strm_path"] = strm_path
                return strm_filename, target_dir, show_root
        except Exception:
            pass

    tmp_path = f"{strm_path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(stream_url)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, strm_path)

    entry["target_dir"] = target_dir
    entry["show_root"] = show_root
    entry["strm_base"] = strm_base_name
    entry["strm_path"] = strm_path
    if entry.get("chat_id") is not None and entry.get("msg_id") is not None:
        try:
            MEDIA_CATALOG.mark_published(
                source_chat_id=int(entry["chat_id"]),
                source_message_id=int(entry["msg_id"]),
            )
            MEDIA_CATALOG.update_job(
                int(entry["chat_id"]), int(entry["msg_id"]), status="published", last_error=""
            )
        except Exception as exc:
            logger.warning(f"[CATALOG] Could not mark published: {type(exc).__name__}: {exc}")
    logger.info(f"[AUTO-SYNC] 🎉 Created .strm file: {strm_filename} -> {strm_path}")
    return strm_filename, target_dir, show_root

async def create_strm_file_async(entry):
    strm_filename, target_dir, show_root = await asyncio.to_thread(_create_strm_file_sync, entry)
    asyncio.create_task(enrich_media_metadata(entry))
    return strm_filename

def create_strm_file(entry):
    strm_filename, target_dir, show_root = _create_strm_file_sync(entry)
    try:
        asyncio.create_task(enrich_media_metadata(entry))
    except Exception:
        pass
    return strm_filename

_last_jf_scan_time = 0.0
_jf_scan_lock = None

async def is_jellyfin_scanning() -> bool:
    """Check if Jellyfin is currently running a ScheduledTasks library scan."""
    headers = _jellyfin_auth_header()
    if not headers:
        return False
    try:
        connector = aiohttp.TCPConnector(family=socket.AF_INET)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get("http://127.0.0.1:8096/ScheduledTasks", headers=headers, timeout=5) as resp:
                if resp.status == 200:
                    tasks = await resp.json()
                    for task in tasks:
                        if task.get("Key") in ("RefreshMediaLibrary", "ScanMediaLibrary") or "Scan Media Library" in str(task.get("Name")):
                            return task.get("State") == "Running"
    except Exception:
        pass
    return False

async def trigger_jellyfin_scan(debounce_seconds: float = 30.0):
    """Trigger Jellyfin Library Scan automatically with debounce, serialization, and retry."""
    global _last_jf_scan_time, _jf_scan_lock
    if _jf_scan_lock is None:
        _jf_scan_lock = asyncio.Lock()
    now = time.monotonic()
    if now - _last_jf_scan_time < debounce_seconds:
        logger.info(f"[JELLYFIN-SCAN] Scan request debounced ({now - _last_jf_scan_time:.1f}s < {debounce_seconds}s)")
        return
    if await is_jellyfin_scanning():
        logger.info("[JELLYFIN-SCAN] Library scan already active in Jellyfin; skipping redundant trigger.")
        return
    async with _jf_scan_lock:
        now = time.monotonic()
        if now - _last_jf_scan_time < debounce_seconds:
            return
        if await is_jellyfin_scanning():
            logger.info("[JELLYFIN-SCAN] Library scan currently active; skipping redundant trigger.")
            return
        _last_jf_scan_time = now
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


async def purge_media_item(
    cache_key: Optional[str] = None,
    chat_id: Optional[int] = None,
    msg_id: Optional[int] = None,
    title_query: Optional[str] = None,
) -> list:
    """Permanently delete a media item from disk, FILE_ID_CACHE, and MEDIA_CATALOG.
    Never removes unrelated media. Returns list of purged titles.
    """
    purged_titles = []
    matched_keys = []

    for key, data in list(FILE_ID_CACHE.items()):
        if not isinstance(data, dict):
            continue
        entry_chat, entry_msg = _split_cache_key(key)
        matched = False

        if cache_key and key == cache_key:
            matched = True
        elif msg_id is not None and entry_msg == int(msg_id):
            if chat_id is None or int(data.get("chat_id") or entry_chat or 0) == int(chat_id):
                matched = True
        elif title_query:
            q = re.sub(r"[^a-z0-9]+", " ", title_query.strip().lower()).strip()
            ent_title = re.sub(r"[^a-z0-9]+", " ", str(data.get("title") or "").lower()).strip()
            ent_clean = re.sub(r"[^a-z0-9]+", " ", str(data.get("clean_title") or "").lower()).strip()
            ent_fname = re.sub(r"[^a-z0-9]+", " ", str(data.get("file_name") or "").lower()).strip()
            if q in (ent_title, ent_clean, ent_fname):
                matched = True
            elif len(q) >= 4 and (ent_title == q or ent_clean == q or ent_title.startswith(q) or ent_clean.startswith(q)):
                matched = True

        if matched:
            matched_keys.append((key, data))

    if not matched_keys:
        return []

    for key, data in matched_keys:
        title = data.get("title") or data.get("clean_title") or key
        strm_path = data.get("strm_path") or ""
        target_dir = data.get("target_dir") or ""
        c_id = data.get("chat_id")
        m_id = data.get("msg_id")
        if not c_id or not m_id:
            entry_chat, entry_msg = _split_cache_key(key)
            c_id = c_id or entry_chat
            m_id = m_id or entry_msg

        # Remove .strm file
        if strm_path and os.path.exists(strm_path):
            try:
                os.remove(strm_path)
                logger.info(f"[PURGE] Deleted STRM file: {strm_path}")
            except OSError as exc:
                logger.warning(f"[PURGE] Could not delete STRM file {strm_path}: {exc}")

        # Safely remove target directory IF genuinely empty or contains only application-generated NFO
        if target_dir and os.path.isdir(target_dir):
            try:
                remaining = os.listdir(target_dir)
                known_app_nfo = {"movie.nfo", "tvshow.nfo"}
                if remaining and all(f.lower() in known_app_nfo or f.lower().endswith(".nfo") for f in remaining):
                    for sf in remaining:
                        try:
                            os.remove(os.path.join(target_dir, sf))
                        except OSError:
                            pass
                if not os.listdir(target_dir):
                    os.rmdir(target_dir)
                    logger.info(f"[PURGE] Deleted empty media directory: {target_dir}")
                else:
                    logger.warning(f"[PURGE] Preserving directory with remaining files: {target_dir}")
            except OSError as exc:
                logger.warning(f"[PURGE] Could not remove media directory {target_dir}: {exc}")

        # Mark deleted in MEDIA_CATALOG so tombstones prevent resurrection across restarts
        if c_id and m_id:
            try:
                MEDIA_CATALOG.mark_deleted(source_chat_id=int(c_id), source_message_id=int(m_id))
            except Exception as exc:
                logger.warning(f"[PURGE] Could not mark deleted in catalog: {exc}")

        # Remove from FILE_ID_CACHE
        FILE_ID_CACHE.pop(key, None)
        purged_titles.append(title)

    if purged_titles:
        await save_cache_async()
        # Trigger Jellyfin scan to immediately drop item from library/UI
        await trigger_jellyfin_scan()

    return purged_titles


async def refresh_jellyfin_item_by_path(strm_path, alt_paths=None, retries=12, delay=5.0):
    """Refresh only the Jellyfin record proven to refer to this .strm path.

    Identity fallback is intentionally fail-closed: it may normalize punctuation
    and duplicate year suffixes, but never removes sequel/part numbers and never
    uses substring title matches. Ambiguous candidates are reported, not guessed.
    """
    if not strm_path:
        return False
    headers = _jellyfin_auth_header()
    if not headers:
        return False
    api_key = _read_jellyfin_api_key()

    expected_root = os.path.normcase(os.path.abspath(SHOWS_DIR if os.path.commonpath(
        [os.path.abspath(strm_path), os.path.abspath(SHOWS_DIR)]
    ) == os.path.abspath(SHOWS_DIR) else MOVIES_DIR))
    expected_type = "Episode" if expected_root == os.path.normcase(os.path.abspath(SHOWS_DIR)) else "Movie"

    def _norm_path(value):
        return os.path.normcase(os.path.abspath(os.path.normpath(str(value or ""))))

    def _base_key(value):
        stem = os.path.splitext(os.path.basename(str(value or "")))[0].lower()
        # Normalize duplicate-year drift only. Keep "Part 1", sequel numbers,
        # and all other meaningful title tokens intact.
        stem = re.sub(r"\((\d{4})\)\s*\(\1\)", r"(\1)", stem)
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", stem)).strip()

    paths = [strm_path]
    if alt_paths:
        paths.extend(p for p in alt_paths if p)
    norm_paths = {_norm_path(p) for p in paths if p}
    target_key = _base_key(strm_path)

    for attempt in range(max(1, int(retries))):
        try:
            connector = aiohttp.TCPConnector(family=socket.AF_INET)
            async with aiohttp.ClientSession(connector=connector) as session:
                params = {
                    "Recursive": "true",
                    "IncludeItemTypes": "Movie,Episode",
                    "Fields": "Path,Type,ProviderIds,ProductionYear,Name,ParentIndexNumber,IndexNumber",
                    "Limit": "10000",
                }
                if api_key:
                    params["api_key"] = api_key
                async with session.get(
                    "http://127.0.0.1:8096/Items",
                    params=params,
                    headers=headers,
                    timeout=30,
                ) as response:
                    if response.status != 200:
                        logger.warning(f"[JELLYFIN-REFRESH] Item lookup failed status={response.status} path='{strm_path}'")
                        await asyncio.sleep(delay)
                        continue
                    payload = await response.json()

                items = [
                    item for item in (payload.get("Items") or [])
                    if item.get("Type") == expected_type and item.get("Id") and item.get("Path")
                ]

                # Exact path (including explicit old paths passed by the mover)
                exact = [item for item in items if _norm_path(item.get("Path")) in norm_paths]
                if exact:
                    matched_items = exact
                    match_reason = "exact_path"
                else:
                    # A basename is usable only if exactly one item of the expected
                    # media type has that same normalized basename across the library.
                    same_base = [item for item in items if target_key and _base_key(item.get("Path")) == target_key]
                    if len(same_base) == 1:
                        matched_items = same_base
                        match_reason = "unique_normalized_basename"
                    elif len(same_base) > 1:
                        logger.warning(
                            f"[JELLYFIN-REFRESH] Ambiguous basename '{os.path.basename(strm_path)}'; "
                            f"candidates={[item.get('Path') for item in same_base[:8]]}; refusing to refresh"
                        )
                        return False
                    else:
                        matched_items = []

                if not matched_items:
                    if attempt > 0 and attempt % 3 == 0:
                        try:
                            await request_jellyfin_scan()
                        except Exception:
                            pass
                    await asyncio.sleep(delay)
                    continue

                # Do not refresh SeriesId/SeasonId for every episode. That causes
                # a refresh storm where every episode repeatedly refreshes its
                # parent series and season while a library scan is running.
                refresh_params = {
                    "metadataRefreshMode": "FullRefresh",
                    "replaceAllMetadata": "true",
                    "replaceAllImages": "false",
                }
                all_ok = True
                for item in matched_items:
                    item_id = str(item["Id"])
                    try:
                        async with session.post(
                            f"http://127.0.0.1:8096/Items/{item_id}/Refresh",
                            params=refresh_params,
                            headers=headers,
                            timeout=15,
                        ) as response:
                            if response.status not in (200, 202, 204):
                                all_ok = False
                                logger.warning(
                                    f"[JELLYFIN-REFRESH] Failed status={response.status} item_id={item_id} "
                                    f"name='{item.get('Name')}' path='{item.get('Path')}'"
                                )
                            else:
                                logger.info(
                                    f"[JELLYFIN-REFRESH] Accepted item_id={item_id} match={match_reason} "
                                    f"path='{item.get('Path')}'"
                                )
                    except Exception as exc:
                        all_ok = False
                        logger.warning(
                            f"[JELLYFIN-REFRESH] Request failed item_id={item_id}: {type(exc).__name__}"
                        )
                return all_ok
        except Exception as exc:
            logger.warning(f"[JELLYFIN-REFRESH] Attempt {attempt + 1} failed: {type(exc).__name__}")
        await asyncio.sleep(delay)

    logger.warning(f"[JELLYFIN-REFRESH] No unambiguous Jellyfin item found for '{strm_path}'; left pending for retry")
    return False

def extract_version_tag(path: str) -> str:
    """Extract resolution and source edition tags from a media filename (e.g. 1080P BLURAY, 720P WEB-DL)."""
    base = os.path.splitext(os.path.basename(path))[0].lower()
    tags = []
    for res in ("2160p", "4k", "1080p", "720p", "480p"):
        if res in base:
            tags.append(res.upper())
            break
    for source in ("bluray", "web-dl", "webdl", "webrip", "remux", "hdtv"):
        if source in base:
            tags.append(source.upper().replace("WEBDL", "WEB-DL"))
            break
    if not tags:
        for codec in ("hevc", "x265", "x264"):
            if codec in base:
                tags.append(codec.upper())
                break
    return " ".join(tags)


async def deduplicate_jellyfin_items():
    """Remove stale/duplicate Jellyfin records for streamer-owned STRM media."""
    headers = _jellyfin_auth_header()
    if not headers:
        return {"removed": 0, "stale": 0}
    api_key = _read_jellyfin_api_key()
    params = {
        "Recursive": "true",
        "IncludeItemTypes": "Movie,Series,Episode",
        "Fields": "Path,ProviderIds",
        "Limit": "10000",
    }
    removed = stale = 0
    try:
        async with aiohttp.ClientSession() as session:
            query = dict(params)
            if api_key:
                query["api_key"] = api_key
            async with session.get(
                "http://127.0.0.1:8096/Items",
                params=query,
                headers=headers,
                timeout=30,
            ) as response:
                if response.status != 200:
                    logger.info(f"[JELLYFIN] Duplicate cleanup skipped (Items status {response.status})")
                    return {"removed": 0, "stale": 0}
                payload = await response.json()
            items = payload.get("Items") or []
            groups = {}
            series_groups = {}
            orphan_items = []

            # Valid cached strm paths
            valid_cached_paths = {
                os.path.normpath(str(d.get("strm_path") or "")).lower()
                for d in FILE_ID_CACHE.values()
                if isinstance(d, dict) and d.get("strm_path")
            }

            for item in items:
                itype = item.get("Type")
                path = os.path.normpath(str(item.get("Path") or ""))
                item_name = str(item.get("Name") or "").strip().lower()
                provider_ids = item.get("ProviderIds") or {}

                is_rogue = is_blacklisted(item_name) or is_blacklisted(path)

                # Blacklisted items are NEVER candidates for Jellyfin item deletion; route to review queue
                if is_rogue:
                    logger.warning(
                        f"[DEDUPLICATE] Item '{item.get('Name')}' (ID: {item.get('Id')}) matched blacklist; "
                        f"routing to review queue without deleting Jellyfin item or media file"
                    )
                    _record_review_job(
                        {
                            "item_id": item.get("Id"),
                            "title": item.get("Name"),
                            "path": path,
                            "type": itype,
                            "provider_ids": provider_ids,
                        },
                        confidence=0.0,
                        reason=f"blacklisted_jellyfin_item:{item.get('Name') or item_name}",
                    )
                    continue

                # 1. Series deduplication
                if itype == "Series":
                    if path and not os.path.exists(path):
                        orphan_items.append((item, path))
                        continue
                    s_name = item_name
                    tmdb_s = provider_ids.get("Tmdb")
                    s_ident = f"series:tmdb:{tmdb_s}" if tmdb_s else f"series:name:{s_name}"
                    series_groups.setdefault(s_ident, []).append((item, path))
                    continue

                if not path.lower().endswith(".strm"):
                    continue
                if not (path.startswith(os.path.normpath(MOVIES_DIR)) or
                        path.startswith(os.path.normpath(SHOWS_DIR))):
                    continue

                # If .strm does not exist on disk, mark immediately as orphan for cleanup
                if not os.path.exists(path):
                    orphan_items.append((item, path))
                    continue

                item_name = str(item.get("Name") or "").strip().lower().replace(" ", "_")
                path_name = os.path.basename(path).lower().replace(" ", "_")
                if item_name in {"unknown_media", "unknown"} or "unknown_media" in path_name:
                    groups.setdefault("unknown-media", []).append((item, path))
                    continue

                is_show_item = (itype == "Episode") or path.startswith(os.path.normpath(SHOWS_DIR))
                if is_show_item:
                    # Episode Identity: MUST be show + season + episode (never merge separate episodes)
                    base_fn = os.path.splitext(os.path.basename(path))[0]
                    parsed = parse_media_meta(base_fn or item.get("Name") or "")
                    s_num = item.get("ParentIndexNumber") or parsed.get("season") or 1
                    e_num = item.get("IndexNumber") or parsed.get("episode") or 1
                    show_name = item.get("SeriesName") or parsed.get("show_name") or os.path.basename(os.path.dirname(os.path.dirname(path)))
                    clean_show = re.sub(r"[^a-z0-9]+", "-", str(show_name or "").lower()).strip("-")
                    identity = f"episode:{clean_show}:s{int(s_num):02d}e{int(e_num):02d}"
                    groups.setdefault(identity, []).append((item, path))
                    continue

                # Movie Identity:
                year_match = re.search(r"\((\d{4})\)", path)
                year_suffix = year_match.group(1) if year_match else ""
                if provider_ids.get("Tmdb"):
                    identity = f"tmdb:{provider_ids['Tmdb']}"
                else:
                    parsed = parse_media_meta(
                        os.path.splitext(os.path.basename(path))[0]
                        or item.get("Name")
                        or ""
                    )
                    dir_lang = os.path.basename(os.path.dirname(os.path.dirname(path)))
                    default_lang = globals().get("DEFAULT_LANGUAGE", "English")
                    detect_lang_fn = globals().get("detect_language")
                    detected_lang = detect_lang_fn(dir_lang) if callable(detect_lang_fn) else None
                    parsed_entry = {
                        **parsed,
                        "language": detected_lang or dir_lang or default_lang,
                    }
                    canonical_key_fn = globals().get("_canonical_media_key")
                    identity = canonical_key_fn(parsed_entry) if callable(canonical_key_fn) else f"name:{parsed.get('clean_title') or 'unknown'}"
                    if identity.endswith(":unknown:" + str(parsed_entry["language"]).lower()):
                        clean_item_name = re.sub(
                            r"[^a-z0-9]+", "-",
                            str(item.get("Name") or "").lower(),
                        ).strip("-")
                        identity = f"name:{clean_item_name}:{year_suffix}"
                groups.setdefault(identity, []).append((item, path))

            duplicates = list(orphan_items)

            # Process Series duplicates
            for s_ident, s_entries in series_groups.items():
                if len(s_entries) > 1:
                    existing_s = [pair for pair in s_entries if os.path.exists(pair[1])]
                    keep_s = sorted(existing_s or s_entries, key=lambda pair: len(pair[1]), reverse=True)[0]
                    norm_keep_s = os.path.normpath(keep_s[1]).lower()
                    for pair in s_entries:
                        if pair is keep_s:
                            continue
                        norm_pair_s = os.path.normpath(pair[1]).lower()
                        if norm_pair_s == norm_keep_s or not os.path.exists(pair[1]):
                            duplicates.append(pair)
                        else:
                            _record_review_job(
                                {"item": pair[0], "path": pair[1], "keep_path": keep_s[1]},
                                confidence=0.0,
                                reason=f"conflicting_series_path:{s_ident}",
                            )

            for identity, entries in groups.items():
                if identity == "unknown-media":
                    for pair in entries:
                        _record_review_job(
                            {"item": pair[0], "path": pair[1]},
                            confidence=0.0,
                            reason="unknown_media_jellyfin_item",
                        )
                    continue
                existing = [pair for pair in entries if os.path.exists(pair[1])]
                keep = sorted(
                    existing or entries,
                    key=lambda pair: (
                        1 if re.search(r"(?: - (?:alt|\d{3,4}p)|\.\d{3,4}p\.)", os.path.basename(pair[1]).lower()) else 0,
                        len(pair[1]),
                    ),
                )[0]
                norm_keep = os.path.normpath(keep[1]).lower()
                keep_dir = os.path.dirname(keep[1])

                for pair in entries:
                    if pair is keep:
                        continue
                    norm_pair = os.path.normpath(pair[1]).lower()
                    if norm_pair == norm_keep:
                        # Identical path duplicate in Jellyfin database
                        duplicates.append(pair)
                    elif not os.path.exists(pair[1]):
                        # Nonexistent ghost path
                        duplicates.append(pair)
                    else:
                        # Existing file on a different path
                        pair_dir = os.path.dirname(pair[1])
                        # If both are in the SAME folder, Jellyfin presents them as alternate versions under one movie card!
                        if os.path.normpath(pair_dir).lower() == os.path.normpath(keep_dir).lower():
                            logger.info(
                                f"[DEDUPLICATE] Retaining alternate version in same directory: {pair[1]}"
                            )
                            continue

                        # If they are in different folders, consolidate alternate movie release into canonical folder:
                        extract_tag_fn = globals().get("extract_version_tag")
                        version_tag = extract_tag_fn(pair[1]) if callable(extract_tag_fn) else ""
                        if (identity.startswith("movie:") or identity.startswith("tmdb:")) and version_tag:
                            base_keep = os.path.splitext(os.path.basename(keep[1]))[0]
                            clean_base = re.sub(r" - (?:alt|\d{3,4}p.*)$", "", base_keep, flags=re.IGNORECASE)
                            target_strm_name = f"{clean_base} - {version_tag}.strm"
                            target_strm_path = os.path.join(keep_dir, target_strm_name)

                            if not os.path.exists(target_strm_path):
                                try:
                                    shutil.move(pair[1], target_strm_path)
                                    logger.info(
                                        f"[DEDUPLICATE] Consolidated alternate movie release: '{pair[1]}' -> '{target_strm_path}'"
                                    )
                                    # Update cache if applicable
                                    save_cache_fn = globals().get("_save_cache_sync")
                                    for cv in FILE_ID_CACHE.values():
                                        if isinstance(cv, dict) and os.path.normpath(str(cv.get("strm_path") or "")).lower() == norm_pair:
                                            cv["strm_path"] = target_strm_path
                                            cv["target_dir"] = keep_dir
                                            if callable(save_cache_fn):
                                                save_cache_fn()
                                            break
                                    try:
                                        os.rmdir(pair_dir)
                                    except OSError:
                                        pass
                                    continue
                                except Exception as move_err:
                                    logger.warning(f"[DEDUPLICATE] Consolidate move notice: {move_err}")

                        # If cannot consolidate cleanly, preserve and route to review queue
                        _record_review_job(
                            {"item": pair[0], "path": pair[1], "keep_path": keep[1]},
                            confidence=0.0,
                            reason=f"conflicting_identity_path:{identity}",
                        )

            for item, path in duplicates:
                item_id = item.get("Id")
                name = item.get("Name") or ""
                if not item_id:
                    continue
                path_exists = os.path.exists(path) if path else False
                dir_path = os.path.dirname(path) if path else ""
                dir_exists = os.path.isdir(dir_path) if dir_path else False

                # CRITICAL AUDIT & SAFETY:
                # 1. We ALWAYS pass deleteFile=false so Jellyfin only removes the metadata row
                #    and NEVER attempts to delete the media file or parent directory from disk.
                # 2. Identical-path duplicates (path_exists=True) remove the duplicate Jellyfin row.
                # 3. Missing/orphan paths (path_exists=False) remove the stale ghost database entry.
                action_desc = "safe_delete_duplicate_db_entry" if path_exists else "safe_delete_stale_ghost_db_entry"
                logger.info(
                    f"[DELETE-AUDIT] Caller=deduplicate_jellyfin_items | item_id={item_id} | name='{name}' | "
                    f"path='{path}' | file_exists={path_exists} | dir_exists={dir_exists} | action='{action_desc}'"
                )
                delete_params = {"api_key": api_key, "deleteFile": "false"} if api_key else {"deleteFile": "false"}
                async with session.delete(
                    f"http://127.0.0.1:8096/Items/{item_id}",
                    params=delete_params,
                    headers=headers,
                    timeout=15,
                ) as response:
                    if response.status in (200, 204):
                        removed += 1
                        if not path_exists:
                            stale += 1
        if removed:
            logger.info(f"[JELLYFIN] Removed {removed} duplicate/stale STRM item(s)")
        return {"removed": removed, "stale": stale}
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        logger.info(f"[JELLYFIN] Duplicate cleanup deferred: {type(exc).__name__}")
        return {"removed": 0, "stale": 0}


async def schedule_jellyfin_cleanup():
    await asyncio.sleep(3)
    await deduplicate_jellyfin_items()

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
        asyncio.create_task(schedule_jellyfin_cleanup())

WEBHOOK_SECRET = get_env("APEX_WEBHOOK_SECRET", "TG_WEBHOOK_SECRET")
STREAM_SIGNING_KEY = get_env("APEX_STREAM_SIGNING_KEY", "APEX_SECRET_KEY", default="")
REQUIRE_SIGNED_STREAMS = get_env("APEX_REQUIRE_SIGNED_STREAMS", default="false").lower() in {
    "1", "true", "yes"
}
RECONCILE_INTERVAL_SECONDS = max(
    300, int(get_env("APEX_RECONCILE_INTERVAL", default="900"))
)

def _is_review_admin(request):
    secret = get_env("APEX_OPS_KEY", "APEX_SECRET_KEY", "TG_REVIEW_ADMIN_KEY", default="")
    auth = request.headers.get("Authorization", "")
    return bool(secret and auth.lower().startswith("bearer ") and
                hmac.compare_digest(auth[7:].strip(), secret))


@routes.get("/review")
@routes.get("/review-queue")
async def review_queue(request):
    """List unhandled ingestion jobs for an authenticated administrator."""
    if not _is_review_admin(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    raw_statuses = request.query.get("status", "review")
    statuses = tuple(s.strip() for s in raw_statuses.split(",") if s.strip())
    if not statuses or any(len(status) > 32 for status in statuses):
        return web.json_response({"ok": False, "error": "invalid status"}, status=400)
    try:
        limit = max(1, min(int(request.query.get("limit", "100")), 1000))
    except ValueError:
        return web.json_response({"ok": False, "error": "invalid limit"}, status=400)
    jobs = MEDIA_CATALOG.list_jobs(statuses=statuses, limit=limit)
    return web.json_response({
        "ok": True,
        "jobs": [
            {
                "id": job.id,
                "source_chat_id": job.source_chat_id,
                "source_message_id": job.source_message_id,
                "canonical_key": job.canonical_key,
                "status": job.status,
                "confidence": job.confidence,
                "retry_count": job.retry_count,
                "next_retry_at": job.next_retry_at,
                "last_error": job.last_error,
                "payload": job.payload,
                "created_at": job.created_at,
                "updated_at": job.updated_at,
            }
            for job in jobs
        ],
    })


def _stream_signature(chat_id, message_id, expires):
    if not STREAM_SIGNING_KEY:
        return ""
    value = f"{int(chat_id)}:{int(message_id)}:{int(expires)}".encode()
    return hmac.new(STREAM_SIGNING_KEY.encode(), value, hashlib.sha256).hexdigest()


def _stream_url(chat_id, message_id):
    return f"http://127.0.0.1:{PORT}/stream/{chat_id}/{message_id}/video.mp4"


@routes.post("/review/{chat_id}/{message_id}/approve")
async def approve_review(request):
    if not _is_review_admin(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    try:
        chat_id = int(request.match_info["chat_id"])
        message_id = int(request.match_info["message_id"])
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "invalid source identity"}, status=400)
    job = MEDIA_CATALOG.find_job(chat_id, message_id)
    if not job or not isinstance(job.payload, dict):
        return web.json_response({"ok": False, "error": "review job not found"}, status=404)
    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError):
        body = {}
    source_name = str(body.get("file_name") or job.payload.get("file_name") or "").strip()
    if not source_name:
        return web.json_response({"ok": False, "error": "file_name is required"}, status=400)
    entry = _meta_to_entry(
        message_id,
        chat_id,
        job.payload.get("file_id"),
        job.payload.get("file_size") or 0,
        source_name,
    )
    FILE_ID_CACHE[_cache_key(chat_id, message_id)] = entry
    _catalog_upsert(entry)
    await create_strm_file_async(entry)
    await save_cache_async()
    MEDIA_CATALOG.update_job(chat_id, message_id, status="approved", last_error="")
    await request_jellyfin_scan()
    return web.json_response({"ok": True, "title": entry.get("title"), "strm_path": entry.get("strm_path")})


@routes.post("/review/{chat_id}/{message_id}/reject")
async def reject_review(request):
    if not _is_review_admin(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    try:
        chat_id = int(request.match_info["chat_id"])
        message_id = int(request.match_info["message_id"])
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "invalid source identity"}, status=400)
    if not MEDIA_CATALOG.update_job(chat_id, message_id, status="rejected", last_error="rejected_by_admin"):
        return web.json_response({"ok": False, "error": "review job not found"}, status=404)
    return web.json_response({"ok": True, "status": "rejected"})

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

        # Intercept AI Admin commands (/ai), private chat messages, and callbacks (ai:*)
        try:
            from ai_admin.telegram_router import handle_webhook_update, is_ai_update
            if is_ai_update(data):
                asyncio.create_task(handle_webhook_update(data, bot_token=BOT_TOKEN, client=tg_app))
                return web.json_response({"ok": True})
        except Exception as _ai_err:
            logger.debug(f"[AI-ADMIN] Webhook check failed: {_ai_err}")

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

        entry = _meta_to_entry(msg_id, chat_id, file_id, file_size, file_name)
        record = _catalog_upsert(entry)
        confidence = _ingestion_confidence(entry)
        if confidence < INGESTION_REVIEW_THRESHOLD:
            _record_review_job(entry, confidence, "unidentified_or_low_confidence")
            return web.json_response({
                "ok": True,
                "status": "review",
                "confidence": confidence,
            })

        if file_id and msg_id:
            if _cache_lookup(msg_id, chat_id):
                return web.json_response({"ok": True, "dedup": True})

            FILE_ID_CACHE[_cache_key(chat_id, msg_id)] = entry
            await save_cache_async()

            strm_name = await create_strm_file_async(entry)
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


_AUTH_CLIENT_HOLDER = {}


@routes.get("/session-login")
async def session_login_page(request):
    """Web interface to generate Pyrogram user string session right inside this space."""
    user_status = "Not Connected"
    user_detail = "No active user session"
    is_connected = False
    if "tg_user" in globals() and tg_user and getattr(tg_user, "is_connected", False):
        try:
            me = await tg_user.get_me()
            is_connected = True
            user_status = "Connected"
            user_detail = f"{getattr(me, 'first_name', '')} (@{getattr(me, 'username', '') or getattr(me, 'id', '')})"
        except Exception:
            user_status = "Connected (Active)"
    
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Telegram User Session Setup</title>
<style>
  :root {{
    --bg-primary: #0b0d13;
    --bg-surface: #141721;
    --bg-elevated: #1a1e2c;
    --border: rgba(255, 255, 255, 0.08);
    --accent: #3b82f6;
    --accent-hover: #2563eb;
    --success: #10b981;
    --danger: #ef4444;
    --text: #f8fafc;
    --text-muted: #94a3b8;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg-primary);
    color: var(--text);
    display: flex;
    justify-content: center;
    align-items: center;
    min-height: 100vh;
    padding: 20px;
  }}
  .card {{
    background: var(--bg-surface);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 32px;
    max-width: 520px;
    width: 100%;
    box-shadow: 0 20px 40px rgba(0,0,0,0.5);
  }}
  h1 {{ font-size: 24px; font-weight: 700; margin-bottom: 8px; }}
  p.subtitle {{ color: var(--text-muted); font-size: 14px; margin-bottom: 24px; line-height: 1.5; }}
  .status-badge {{
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 6px 12px;
    border-radius: 999px;
    font-size: 13px;
    font-weight: 600;
    margin-bottom: 24px;
    background: {'rgba(16, 185, 129, 0.15)' if is_connected else 'rgba(239, 68, 68, 0.15)'};
    color: {'var(--success)' if is_connected else 'var(--danger)'};
    border: 1px solid {'rgba(16, 185, 129, 0.3)' if is_connected else 'rgba(239, 68, 68, 0.3)'};
  }}
  .form-group {{ margin-bottom: 16px; text-align: left; }}
  label {{ display: block; font-size: 13px; font-weight: 600; color: var(--text-muted); margin-bottom: 6px; }}
  input {{
    width: 100%;
    padding: 12px 14px;
    border-radius: 8px;
    background: var(--bg-elevated);
    border: 1px solid var(--border);
    color: var(--text);
    font-size: 15px;
    outline: none;
    transition: border-color 0.2s;
  }}
  input:focus {{ border-color: var(--accent); }}
  button {{
    width: 100%;
    padding: 12px 16px;
    border-radius: 8px;
    background: var(--accent);
    color: #fff;
    border: none;
    font-size: 15px;
    font-weight: 600;
    cursor: pointer;
    transition: background 0.2s;
    margin-top: 8px;
  }}
  button:hover {{ background: var(--accent-hover); }}
  button:disabled {{ opacity: 0.5; cursor: not-allowed; }}
  .alert {{
    padding: 12px 14px;
    border-radius: 8px;
    font-size: 13px;
    margin-bottom: 16px;
    line-height: 1.4;
    display: none;
  }}
  .alert-error {{ background: rgba(239, 68, 68, 0.15); color: #fca5a5; border: 1px solid rgba(239, 68, 68, 0.3); }}
  .alert-success {{ background: rgba(16, 185, 129, 0.15); color: #6ee7b7; border: 1px solid rgba(16, 185, 129, 0.3); }}
  .step-2 {{ display: none; }}
  .result-box {{
    margin-top: 20px;
    padding: 16px;
    background: var(--bg-elevated);
    border-radius: 8px;
    border: 1px solid var(--border);
    display: none;
  }}
  .result-box textarea {{
    width: 100%;
    height: 90px;
    background: #0b0d13;
    border: 1px solid var(--border);
    color: #10b981;
    font-family: monospace;
    font-size: 11px;
    padding: 8px;
    border-radius: 6px;
    resize: none;
    margin-top: 8px;
  }}
</style>
</head>
<body>
<div class="card">
  <h1>⚡ Automatic Userbot Setup</h1>
  <p class="subtitle">Log in once with your phone number to automatically unlock full channel history scanning & zero-reforward playback.</p>

  <div class="status-badge">
    <span>●</span> Status: {user_status} ({user_detail})
  </div>

  <div id="alertBox" class="alert"></div>

  <!-- Step 1: Phone -->
  <div id="stepPhone">
    <div class="form-group">
      <label>Phone Number (with Country Code)</label>
      <input type="text" id="phoneInput" placeholder="+919876543210" autocomplete="tel">
    </div>
    <button id="sendBtn" onclick="sendCode()">Send Telegram OTP</button>
  </div>

  <!-- Step 2: Code & 2FA -->
  <div id="stepCode" class="step-2">
    <div class="form-group">
      <label>Telegram Login OTP Code</label>
      <input type="text" id="codeInput" placeholder="Enter code sent by Telegram">
    </div>
    <div class="form-group" id="passGroup" style="display:none;">
      <label>2-Step Verification Password (if enabled)</label>
      <input type="password" id="passInput" placeholder="Your 2FA cloud password">
    </div>
    <button id="verifyBtn" onclick="verifyCode()">Verify & Activate Userbot</button>
  </div>

  <!-- Step 3: Success -->
  <div id="resultBox" class="result-box">
    <p style="font-weight:600;color:var(--success);margin-bottom:6px;">✅ Session Generated & Userbot Activated!</p>
    <p style="font-size:12px;color:var(--text-muted);">Saved to storage volume! Copy this into your Hugging Face Secrets as <code>USER_SESSION_STRING</code> for permanent persistence:</p>
    <textarea id="sessionText" readonly></textarea>
    <button onclick="copySession()" style="margin-top:8px;background:var(--bg-elevated);border:1px solid var(--border);">📋 Copy to Clipboard</button>
  </div>
</div>

<script>
async function sendCode() {{
  const phone = document.getElementById('phoneInput').value.trim();
  const alertBox = document.getElementById('alertBox');
  const btn = document.getElementById('sendBtn');
  if (!phone) {{
    showAlert('Please enter your phone number with country code.', 'error');
    return;
  }}
  btn.disabled = true;
  btn.innerText = 'Sending...';
  hideAlert();
  try {{
    const res = await fetch('/tg-stream/auth/send-code', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ phone }})
    }});
    const data = await res.json();
    if (data.ok) {{
      showAlert('Login code sent to your Telegram app!', 'success');
      document.getElementById('stepPhone').style.display = 'none';
      document.getElementById('stepCode').style.display = 'block';
    }} else {{
      showAlert(data.error || 'Failed to send code', 'error');
    }}
  }} catch (err) {{
    showAlert('Network error: ' + err.message, 'error');
  }} finally {{
    btn.disabled = false;
    btn.innerText = 'Send Telegram OTP';
  }}
}}

async function verifyCode() {{
  const code = document.getElementById('codeInput').value.trim();
  const password = document.getElementById('passInput').value;
  const btn = document.getElementById('verifyBtn');
  if (!code) {{
    showAlert('Please enter the OTP code received from Telegram.', 'error');
    return;
  }}
  btn.disabled = true;
  btn.innerText = 'Verifying...';
  hideAlert();
  try {{
    const res = await fetch('/tg-stream/auth/verify-code', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ code, password }})
    }});
    const data = await res.json();
    if (data.ok) {{
      showAlert('Success! User session activated and scanning started.', 'success');
      document.getElementById('stepCode').style.display = 'none';
      document.getElementById('resultBox').style.display = 'block';
      document.getElementById('sessionText').value = data.session_string;
    }} else if (data.need_password) {{
      showAlert('2-Step Verification password required.', 'error');
      document.getElementById('passGroup').style.display = 'block';
    }} else {{
      showAlert(data.error || 'Verification failed', 'error');
    }}
  }} catch (err) {{
    showAlert('Network error: ' + err.message, 'error');
  }} finally {{
    btn.disabled = false;
    btn.innerText = 'Verify & Activate Userbot';
  }}
}}

function showAlert(msg, type) {{
  const b = document.getElementById('alertBox');
  b.innerText = msg;
  b.className = 'alert alert-' + type;
  b.style.display = 'block';
}}
function hideAlert() {{
  document.getElementById('alertBox').style.display = 'none';
}}
function copySession() {{
  const t = document.getElementById('sessionText');
  t.select();
  navigator.clipboard.writeText(t.value);
  showAlert('Copied session string to clipboard!', 'success');
}}
</script>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html")


@routes.post("/auth/send-code")
async def auth_send_code(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid json"}, status=400)
    phone = (data.get("phone") or "").strip()
    if not phone:
        return web.json_response({"ok": False, "error": "Phone number required"}, status=400)
    if not API_ID or not API_HASH:
        return web.json_response({"ok": False, "error": "API_ID and API_HASH not configured on server"}, status=500)

    old = _AUTH_CLIENT_HOLDER.get("client")
    if old:
        try:
            await old.stop()
        except Exception:
            pass
        _AUTH_CLIENT_HOLDER.clear()

    try:
        temp_client = Client(
            "temp_session_gen",
            api_id=int(API_ID),
            api_hash=API_HASH,
            in_memory=True,
        )
        await temp_client.connect()
        sent_code = await temp_client.send_code(phone)
        _AUTH_CLIENT_HOLDER["client"] = temp_client
        _AUTH_CLIENT_HOLDER["phone"] = phone
        _AUTH_CLIENT_HOLDER["phone_code_hash"] = sent_code.phone_code_hash
        return web.json_response({"ok": True})
    except Exception as e:
        logger.error(f"[AUTH] send_code failed: {e}")
        return web.json_response({"ok": False, "error": str(e)}, status=400)


@routes.post("/auth/verify-code")
async def auth_verify_code(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid json"}, status=400)
    code = (data.get("code") or "").strip()
    password = data.get("password") or ""

    temp_client = _AUTH_CLIENT_HOLDER.get("client")
    phone = _AUTH_CLIENT_HOLDER.get("phone")
    phone_code_hash = _AUTH_CLIENT_HOLDER.get("phone_code_hash")
    if not temp_client or not phone or not phone_code_hash:
        return web.json_response({"ok": False, "error": "No pending login session. Send code first."}, status=400)

    try:
        from pyrogram.errors import SessionPasswordNeeded, PasswordHashInvalid, PhoneCodeInvalid, PhoneCodeExpired
    except Exception:
        SessionPasswordNeeded = Exception
        PasswordHashInvalid = Exception
        PhoneCodeInvalid = Exception
        PhoneCodeExpired = Exception

    try:
        try:
            await temp_client.sign_in(phone, phone_code_hash, code)
        except SessionPasswordNeeded:
            if not password:
                return web.json_response({"ok": False, "need_password": True})
            await temp_client.check_password(password)

        session_str = await temp_client.export_session_string()
        
        session_file = os.path.join(DATA_DIR, "user_session.string")
        try:
            with open(session_file, "w", encoding="utf-8") as f:
                f.write(session_str)
            logger.info(f"[AUTH] Saved new session string to {session_file}")
        except Exception as se:
            logger.warning(f"[AUTH] Could not persist session file: {se}")

        global USER_SESSION_STRING, tg_user
        USER_SESSION_STRING = session_str
        if "tg_user" in globals() and tg_user and getattr(tg_user, "is_connected", False):
            try:
                await tg_user.stop()
            except Exception:
                pass

        tg_user = Client(
            "tg_user_session",
            api_id=int(API_ID),
            api_hash=API_HASH,
            session_string=session_str,
            workdir=DATA_DIR,
            in_memory=True,
            no_updates=True,
            max_concurrent_transmissions=APEX_CFG.telegram_max_concurrent,
        )
        await tg_user.start()
        user_me = await tg_user.get_me()
        logger.info(f"[PYROGRAM] Pyrogram User Account client activated! Logged in as: {getattr(user_me, 'first_name', '')}")

        try:
            await temp_client.stop()
        except Exception:
            pass
        _AUTH_CLIENT_HOLDER.clear()

        asyncio.create_task(_sync_configured_channel_once())

        return web.json_response({"ok": True, "session_string": session_str})

    except (PhoneCodeInvalid, PhoneCodeExpired) as e:
        return web.json_response({"ok": False, "error": f"Invalid or expired OTP code: {e}"}, status=400)
    except PasswordHashInvalid as e:
        return web.json_response({"ok": False, "need_password": True, "error": "Incorrect 2FA password"}, status=400)
    except Exception as e:
        logger.error(f"[AUTH] verify_code error: {e}")
        return web.json_response({"ok": False, "error": str(e)}, status=400)


async def _process_and_index_message(message, chat_id):
    """Ingest a single Pyrogram message into FILE_ID_CACHE and create .strm."""
    if not message or getattr(message, "empty", False):
        return False, False
    media = message.video or message.document or message.audio or message.animation
    if not media or not getattr(media, "file_id", None):
        return False, False
    chat = message.chat
    msg_chat_id = chat.id if chat else chat_id
    if msg_chat_id is None or not allowed_chat(chat):
        return False, False
    msg_id = message.id
    file_name = media_file_name(message, media)
    caption = getattr(message, "caption", None)
    if is_blacklisted(file_name) or is_blacklisted(caption):
        logger.info(f"[BLACKLIST] Routing blacklisted media to review queue during reindex: {file_name}")
        _record_review_job({"file_name": file_name, "caption": caption, "chat_id": msg_chat_id, "msg_id": msg_id}, confidence=0.0, reason=f"blacklisted_message:{file_name}")
        return False, False
    existing = _cache_lookup(msg_id, msg_chat_id)
    if isinstance(existing, dict):
        if is_blacklisted(existing.get("title")) or is_blacklisted(existing.get("file_name")):
            _record_review_job(existing, confidence=0.0, reason=f"blacklisted_reindex:{existing.get('title') or existing.get('file_name')}")
            return False, False
        strm_path = existing.get("strm_path") or _entry_strm_path(existing)
        if strm_path and os.path.exists(strm_path):
            if media.file_id and existing.get("file_id") != media.file_id:
                existing["file_id"] = media.file_id
                FILE_ID_CACHE[_cache_key(msg_chat_id, msg_id)] = existing
            return False, True
    if subtitle_is_document(file_name, getattr(media, "mime_type", None)):
        placed = await _place_subtitle(msg_id, msg_chat_id, media.file_id, file_name)
        logger.info(f"[REINDEX] Subtitle handled: {file_name} placed={placed}")
        return False, True
    entry = _meta_to_entry(msg_id, msg_chat_id, media.file_id, media.file_size or 0, file_name)
    if _is_unknown_media(entry):
        return False, True
    if isinstance(existing, dict):
        for k in ("language", "tmdb_id", "tmdb_title", "tmdb_year", "target_dir", "strm_path", "strm_base", "show_root"):
            if existing.get(k):
                entry[k] = existing[k]
    FILE_ID_CACHE[_cache_key(msg_chat_id, msg_id)] = entry
    strm_name = await create_strm_file_async(entry)
    logger.info(f"[REINDEX] Re-ingested '{strm_name}' (msg {msg_id}, chat {msg_chat_id})")
    return True, False


async def _scan_chat_history(chat_id, limit, indexed_list, from_id=None, to_id=None):
    """Scan one chat's history and re-ingest every media message into FILE_ID_CACHE + .strm.

    Supports:
    1. User account client (tg_user) which has unrestricted access to full channel history.
    2. Explicit ID range scan (from_id to to_id) via get_messages().
    3. Standard get_chat_history() with automatic fallback to sequential ID scanning for bots.
    """
    indexed = 0
    skipped = 0
    scanner = tg_user if ("tg_user" in globals() and tg_user and getattr(tg_user, "is_connected", False)) else tg_app
    if not scanner:
        indexed_list.append({"chat_id": chat_id, "indexed": 0, "skipped": 0})
        return

    # Mode 1: Explicit ID range scan (e.g. from_id=1 to_id=500)
    if from_id is not None and to_id is not None:
        start_id = min(int(from_id), int(to_id))
        end_id = max(int(from_id), int(to_id))
        all_ids = list(range(start_id, end_id + 1))
        for i in range(0, len(all_ids), 50):
            batch_ids = all_ids[i:i + 50]
            try:
                msgs = await scanner.get_messages(chat_id, batch_ids)
                if not isinstance(msgs, list):
                    msgs = [msgs]
                for msg in msgs:
                    did_idx, did_skp = await _process_and_index_message(msg, chat_id)
                    if did_idx:
                        indexed += 1
                    elif did_skp:
                        skipped += 1
            except Exception as e:
                logger.warning(f"[REINDEX] ID range batch {batch_ids[0]}..{batch_ids[-1]} failed on {chat_id}: {e}")
            await asyncio.sleep(0.05)
        indexed_list.append({"chat_id": chat_id, "indexed": indexed, "skipped": skipped, "range": f"{start_id}..{end_id}"})
        return

    # Mode 2: get_chat_history (works for user accounts, or channels with bot history rights)
    try:
        async for message in scanner.get_chat_history(chat_id, limit=limit):
            did_idx, did_skp = await _process_and_index_message(message, chat_id)
            if did_idx:
                indexed += 1
            elif did_skp:
                skipped += 1
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        if "BOT_METHOD_INVALID" in detail or "GetHistory" in detail:
            # Standard Telegram bots cannot call GetHistory, but admin bots CAN call get_messages by ID!
            known_ids = [
                int(e.get("message_id") or e.get("msg_id"))
                for e in FILE_ID_CACHE.values()
                if str(e.get("chat_id")) == str(chat_id) and (e.get("message_id") or e.get("msg_id"))
            ]
            max_id = max(known_ids) if known_ids else max(50, limit)
            min_id = max(1, max_id - limit)
            logger.info(f"[REINDEX] Bot history fallback: checking IDs {min_id}..{max_id} via get_messages()")
            all_ids = list(range(min_id, max_id + 1))
            for i in range(0, len(all_ids), 50):
                batch_ids = all_ids[i:i + 50]
                try:
                    msgs = await scanner.get_messages(chat_id, batch_ids)
                    if not isinstance(msgs, list):
                        msgs = [msgs]
                    for msg in msgs:
                        did_idx, did_skp = await _process_and_index_message(msg, chat_id)
                        if did_idx:
                            indexed += 1
                        elif did_skp:
                            skipped += 1
                except Exception as be:
                    logger.debug(f"[REINDEX] Batch ID scan error on {chat_id}: {be}")
                await asyncio.sleep(0.05)
        else:
            raise
    indexed_list.append({"chat_id": chat_id, "indexed": indexed, "skipped": skipped})


async def _sync_configured_channel_once():
    """Recover recent channel posts when webhook delivery is delayed or missed."""
    if (not tg_app or not tg_app.is_connected) and (not tg_user or not getattr(tg_user, "is_connected", False)):
        return
    targets = []
    for raw in (RAW_CHANNEL_ID or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            targets.append(int(raw) if raw.lstrip("-").isdigit() else raw)
        except ValueError:
            logger.warning(f"[SYNC] Ignoring invalid configured chat ID: {raw!r}")
    if not targets:
        try:
            targets = await _resolve_reindex_targets(None)
        except Exception as exc:
            logger.info(f"[SYNC] Dialog discovery unavailable: {type(exc).__name__}: {exc}")
            return
        if not targets:
            return
    results = []
    for chat_id in targets:
        try:
            sync_limit = int(os.environ.get("SYNC_HISTORY_LIMIT", "1000" if (tg_user and getattr(tg_user, "is_connected", False)) else "100"))
            await _scan_chat_history(chat_id, limit=sync_limit, indexed_list=results)
        except FloodWait as exc:
            logger.info(f"[SYNC] FloodWait for {chat_id}; retrying after {exc.value}s")
            await asyncio.sleep(exc.value)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            if "BOT_METHOD_INVALID" in detail or "GetHistory" in detail:
                logger.info(
                    f"[SYNC] Telegram bot accounts cannot read historical messages from "
                    f"{chat_id}; waiting for new webhook/channel updates."
                )
                continue
            logger.info(
                f"[SYNC] Recent history unavailable for {chat_id}: {detail}"
            )
    indexed = sum(item.get("indexed", 0) for item in results)
    if indexed:
        await save_cache_async()
        await trigger_jellyfin_scan()
        logger.info(f"[SYNC] Recovered {indexed} recent media item(s) from configured channel(s)")


async def _configured_channel_sync_loop():
    await asyncio.sleep(15)
    while True:
        try:
            await _sync_configured_channel_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"[SYNC] Recent channel sync failed: {type(exc).__name__}: {exc}")
        await asyncio.sleep(120)


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
            logger.info(f"[REINDEX] Skipping unavailable configured chat '{raw}': {type(e).__name__}")

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
            logger.info(f"[REINDEX] Dialog discovery unavailable for bot session: {type(e).__name__}")
    return list(targets)


@routes.post("/reindex")
@routes.get("/reindex")
async def reindex_route(request):
    """Re-scan the bot's channels + DMs and re-ingest every media message.

    Rebuilds FILE_ID_CACHE and .strm files from Telegram chat history — useful after a
    fresh deploy where file_ids.json was empty (ingestion only receives NEW updates).
    """
    if not _is_review_admin(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    if (not tg_app or not tg_app.is_connected) and (not tg_user or not getattr(tg_user, "is_connected", False)):
        return web.json_response({"ok": False, "error": "Pyrogram client not connected"}, status=503)
    try:
        limit = int(request.query.get("limit", "200"))
    except (TypeError, ValueError):
        limit = 200
    explicit = request.query.get("chat_id")
    from_id = request.query.get("from_id")
    to_id = request.query.get("to_id")

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
            await _scan_chat_history(chat_id, limit, results, from_id=from_id, to_id=to_id)
        except FloodWait as e:
            logger.info(f"[REINDEX] FloodWait on chat {chat_id}: sleeping {e.value}s")
            await asyncio.sleep(e.value)
            try:
                await _scan_chat_history(chat_id, limit, results, from_id=from_id, to_id=to_id)
            except Exception as e2:
                logger.warning(f"[REINDEX] chat {chat_id} retry failed: {e2}")
                failures.append({"chat_id": chat_id, "error": f"{type(e2).__name__}: {e2}"})
        except Exception as e:
            detail = f"{type(e).__name__}: {e}"
            if "BOT_METHOD_INVALID" in detail or "Peer id invalid" in detail:
                logger.info(f"[REINDEX] Skipping chat {chat_id}: history is unavailable to this bot session")
                failures.append({"chat_id": chat_id, "skipped": True, "reason": "history unavailable to bot session"})
            else:
                logger.warning(f"[REINDEX] chat {chat_id} failed: {detail}")
                failures.append({"chat_id": chat_id, "error": detail})

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
    # Defensive: if chat_id > 0 and message_id < 0, they were inverted in the URL path
    if chat_id > 0 and message_id < 0:
        chat_id, message_id = message_id, chat_id
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
@routes.get("/stream/{chat_id}/{message_id}/{filename}")
@routes.head("/stream_file")
@routes.head("/stream/{message_id}")
@routes.head("/stream/{message_id}/{filename}")
@routes.head("/stream/{chat_id}/{message_id}/video.mp4")
@routes.head("/stream/{chat_id}/{message_id}/{filename}")
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
    is_loopback = (
        request.remote in ("127.0.0.1", "::1", "localhost")
        or request.host.startswith("127.0.0.1:")
        or request.host.startswith("localhost:")
        or request.host.split(":")[0] in ("127.0.0.1", "localhost", "::1")
    )
    if STREAM_SIGNING_KEY and not is_loopback:
        try:
            expires = int(request.query.get("expires", "0"))
            signature = request.query.get("sig", "")
        except (TypeError, ValueError):
            expires, signature = 0, ""
        expected = _stream_signature(chat_id, message_id, expires)
        alt_expected = _stream_signature(message_id, chat_id, expires)
        sig_matches = (
            signature and (
                hmac.compare_digest(signature, expected) or
                hmac.compare_digest(signature, alt_expected)
            )
        )
        if (REQUIRE_SIGNED_STREAMS or signature or expires) and (
            expires < int(time.time()) or
            not signature or
            not sig_matches
        ):
            return web.Response(status=403, text="Invalid or expired stream token.")

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


@routes.get("/diagnostics/{chat_id}/{message_id}")
async def stream_diagnostics(request):
    """Return admin-only source, catalog, pool, and cache diagnostics."""
    if not _is_review_admin(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    try:
        chat_id = int(request.match_info["chat_id"])
        message_id = int(request.match_info["message_id"])
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "invalid source identity"}, status=400)
    entry = _cache_lookup(message_id, chat_id)
    record = MEDIA_CATALOG.find_by_source(chat_id, message_id)
    job = MEDIA_CATALOG.find_job(chat_id, message_id)
    return web.json_response({
        "ok": True,
        "source": {"chat_id": chat_id, "message_id": message_id},
        "indexed": isinstance(entry, dict),
        "entry": {
            "title": entry.get("title"),
            "file_name": entry.get("file_name"),
            "language": entry.get("language"),
            "is_tv": entry.get("is_tv"),
            "strm_path": entry.get("strm_path"),
        } if isinstance(entry, dict) else None,
        "catalog": {
            "canonical_key": record.canonical_key,
            "published": record.published,
            "deleted": record.deleted,
        } if record else None,
        "job": {
            "status": job.status,
            "confidence": job.confidence,
            "retry_count": job.retry_count,
            "last_error": job.last_error,
        } if job else None,
        "pool": apex_pool.snapshot(),
        "cache": apex_cache.stats(),
    })


@routes.get("/metrics")
async def metrics(request):
    return web.json_response(apex_metrics.snapshot())


@routes.post("/republish")
@routes.get("/republish")
async def republish(request):
    """Recreate a Jellyfin publication without downloading the Telegram file."""
    if not _is_review_admin(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    raw_message = request.query.get("message_id")
    raw_chat = request.query.get("chat_id")
    if raw_message is None:
        return web.json_response({"ok": False, "error": "message_id is required"}, status=400)
    try:
        message_id = int(raw_message)
        chat_id = int(raw_chat) if raw_chat is not None else None
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "invalid source identity"}, status=400)
    entry = _cache_lookup(message_id, chat_id)
    if not isinstance(entry, dict):
        return web.json_response({"ok": False, "error": "media not indexed"}, status=404)
    entry = dict(entry)
    MEDIA_CATALOG.unpublish(
        source_chat_id=int(entry.get("chat_id") or chat_id or 0),
        source_message_id=int(entry.get("msg_id") or message_id),
    )
    FILE_ID_CACHE[_cache_key(entry.get("chat_id") or chat_id or 0, message_id)] = entry
    await create_strm_file_async(entry)
    await save_cache_async()
    await request_jellyfin_scan()
    return web.json_response({"ok": True, "title": entry.get("title"), "strm_path": entry.get("strm_path")})


@routes.post("/reconcile")
@routes.get("/reconcile")
async def reconcile(request):
    """Repair missing publications while leaving explicit catalog tombstones alone."""
    if not _is_review_admin(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    dry_run = request.query.get("dry_run", "").lower() in ("1", "true", "yes")
    repaired = 0
    skipped_deleted = 0
    would_repair = []
    for key, data in list(FILE_ID_CACHE.items()):
        if not isinstance(data, dict):
            continue
        entry = dict(data)
        if _is_unknown_media(entry):
            logger.warning(f"[RECONCILE] Preserving unknown media entry on disk and routing to review queue: {key}")
            _record_review_job(entry, confidence=0.0, reason="unknown_media_reconcile")
            continue
        path = entry.get("strm_path") or _entry_strm_path(entry)
        record = MEDIA_CATALOG.find_by_source(
            int(entry.get("chat_id") or 0), int(entry.get("msg_id") or 0)
        )
        if record is not None and record.deleted:
            skipped_deleted += 1
            continue
        if not os.path.exists(path):
            if dry_run:
                would_repair.append(entry.get("title") or path)
            else:
                await create_strm_file_async(entry)
                FILE_ID_CACHE[key] = entry
            repaired += 1
    if repaired and not dry_run:
        await save_cache_async()
        await request_jellyfin_scan()
    cleanup = await deduplicate_jellyfin_items() if not dry_run else {"dry_run": True}
    return web.json_response({
        "ok": True,
        "dry_run": dry_run,
        "repaired": repaired,
        "would_repair": would_repair if dry_run else None,
        "skipped_deleted": skipped_deleted,
        "jellyfin_cleanup": cleanup,
    })


async def backfill_missing_subtitles(force: bool = False):
    """Scan all indexed media in FILE_ID_CACHE and automatically inject missing subtitles."""
    logger.info("[SUBTITLES] Starting automated subtitle backfill scan across all library items...")
    total_injected = 0
    connector = aiohttp.TCPConnector(family=socket.AF_INET)
    async with aiohttp.ClientSession(connector=connector) as session:
        for key, entry in list(FILE_ID_CACHE.items()):
            if not isinstance(entry, dict):
                continue
            target_dir = entry.get("target_dir") or strm_dir(MOVIES_DIR, SHOWS_DIR, entry)
            base = entry.get("strm_base") or strm_base(entry)
            if not target_dir or not base:
                continue

            # Check if primary subtitle (.eng.srt) already exists and is valid
            eng_sub = os.path.join(target_dir, f"{base}.eng.srt")
            en_sub = os.path.join(target_dir, f"{base}.en.srt")
            has_valid_sub = is_valid_subtitle_file(eng_sub) or is_valid_subtitle_file(en_sub)
            
            # If entry was previously mismatched to Generation Iron, force re-injection
            raw_check = (entry.get("file_name") or entry.get("title") or "").lower()
            entry_force = force
            if "iron" in raw_check and "man" in raw_check:
                if entry.get("tmdb_id") == 210879 or "generation iron" in (entry.get("tmdb_title") or "").lower():
                    entry_force = True
            if not entry_force and has_valid_sub:
                continue

            # Check if video .strm exists
            strm_path = entry.get("strm_path") or os.path.join(target_dir, f"{base}.strm")
            if not os.path.exists(strm_path):
                continue

            # If IMDb ID is missing, try resolving from TMDB detail
            if not entry.get("imdb_id") and entry.get("tmdb_id"):
                try:
                    if entry.get("is_tv"):
                        ext_ids = await _tmdb_get(f"/tv/{entry['tmdb_id']}/external_ids", {})
                        entry["imdb_id"] = ext_ids.get("imdb_id") if isinstance(ext_ids, dict) else None
                    else:
                        m_detail = await _tmdb_get(f"/movie/{entry['tmdb_id']}", {})
                        entry["imdb_id"] = m_detail.get("imdb_id") if isinstance(m_detail, dict) else None
                    if entry.get("imdb_id"):
                        FILE_ID_CACHE[key]["imdb_id"] = entry["imdb_id"]
                        await save_cache_async()
                except Exception:
                    pass

            try:
                injected = await inject_subtitles_for_entry(session, entry, force=entry_force)
                if injected:
                    total_injected += len(injected)
                    logger.info(f"[SUBTITLE-BACKFILL] Injected {len(injected)} subtitle(s) for '{entry.get('title')}'")
                    for s_path in injected:
                        s_name = os.path.basename(s_path)
                        SUBTITLE_CACHE[f"auto_{entry.get('msg_id')}_{s_name}"] = {
                            "file_name": s_name,
                            "placed": True,
                            "target": s_path,
                            "video_msg": entry.get("msg_id"),
                        }
                    await save_subtitle_cache_async()
                    # Refresh Jellyfin item so subtitle shows immediately
                    await refresh_jellyfin_item_by_path(strm_path)
                    # Polite rate pacing between downloads
                    await asyncio.sleep(1.0)
            except Exception as e:
                logger.warning(f"[SUBTITLE-BACKFILL] Error for '{entry.get('title')}': {e}")

    logger.info(f"[SUBTITLES] Completed subtitle backfill scan: {total_injected} subtitle(s) injected.")
    return total_injected


@routes.get("/subtitles/status")
async def subtitles_status(request):
    """Return summary of subtitle coverage across media in library."""
    total_items = 0
    with_subtitles = 0
    missing_subtitles = 0
    for key, entry in FILE_ID_CACHE.items():
        if not isinstance(entry, dict):
            continue
        total_items += 1
        target_dir = entry.get("target_dir") or strm_dir(MOVIES_DIR, SHOWS_DIR, entry)
        base = entry.get("strm_base") or strm_base(entry)
        has_sub = False
        if target_dir and base and os.path.exists(target_dir):
            for f in os.listdir(target_dir):
                if f.lower().endswith((".srt", ".vtt", ".ass")) and base.lower() in f.lower():
                    if is_valid_subtitle_file(os.path.join(target_dir, f)):
                        has_sub = True
                        break
        if has_sub:
            with_subtitles += 1
        else:
            missing_subtitles += 1

    return web.json_response({
        "ok": True,
        "total_items": total_items,
        "with_subtitles": with_subtitles,
        "missing_subtitles": missing_subtitles,
        "coverage_percentage": round((with_subtitles / max(1, total_items)) * 100, 1),
    })


@routes.post("/subtitles/sync")
@routes.get("/subtitles/sync")
async def subtitles_sync(request):
    """Trigger background backfill scan for missing subtitles."""
    asyncio.create_task(backfill_missing_subtitles())
    return web.json_response({
        "ok": True,
        "message": "Subtitle synchronization triggered in background",
    })


async def _prune_orphan_strms(movies_dir, shows_dir, keep):
    """Delete .strm files that point at our streamer but aren't in `keep`.

    During a folder-layout migration Jellyfin indexes both the stale flat .strm
    and the new per-title folder .strm for the same Telegram file -> duplicates.
    Only files whose contents reference our own streamer are touched, so
    unrelated .strm files are never removed.
    """
    removed = 0
    for root_dir in (movies_dir, shows_dir):
        if not os.path.isdir(root_dir):
            continue
        for root, _, files in os.walk(root_dir):
            for f in files:
                if not f.lower().endswith(".strm"):
                    continue
                path = os.path.normpath(os.path.join(root, f))
                if path in keep:
                    continue
                try:
                    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                        content = fh.read(256)
                except OSError:
                    continue
                stream_port = os.environ.get(
                    "TG_LISTEN_PORT", os.environ.get("PORT", "8080")
                )
                is_our_stream = (
                    "/stream/" in content
                    or f"127.0.0.1:{stream_port}" in content
                    or f":{stream_port}/" in content
                )
                if not is_our_stream:
                    continue
                try:
                    os.remove(path)
                    removed += 1
                    logger.info(f"[RESTORE] Pruned orphaned .strm: {path}")
                    parent_dir = os.path.dirname(path)
                    if os.path.isdir(parent_dir) and not os.listdir(parent_dir):
                        try:
                            os.rmdir(parent_dir)
                            logger.info(f"[RESTORE] Cleaned empty parent folder: {parent_dir}")
                        except OSError:
                            pass
                except OSError as e:
                    logger.warning(f"[RESTORE] Could not prune {path}: {e}")
    return removed


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
                    elif '?' in content:
                        cleaned = content.split('?')[0].strip()
                        if cleaned and cleaned != content:
                            with open(path, 'w', encoding='utf-8') as f_out:
                                f_out.write(cleaned)
                            logger.info(f'[RESTORE] Sanitized query token in .strm: {path}')
                except Exception:
                    pass
    if removed:
        logger.info(f"[MIGRATION] Removed {removed} obsolete Go-era .strm file(s)")

    count = 0
    expected_paths = set()
    restore_candidates = []
    stale_cache_keys = []
    for key, data in FILE_ID_CACHE.items():
        if not isinstance(data, dict):
            continue
        _chat_from_key, msg_id = _split_cache_key(key)
        file_id = data.get("file_id")
        # Prefer the original Telegram filename. Re-parsing the display title
        # loses the year/release information and can recreate a second folder
        # such as "Spiderman Homecoming" beside "Spiderman Homecoming (2017)".
        source_name = data.get("file_name") or data.get("title") or f"Media_{msg_id}"
        if not file_id or not source_name:
            continue
        chat_id = data.get("chat_id") or _chat_from_key
        entry = _meta_to_entry(msg_id, chat_id, file_id, data.get("file_size") or 0, source_name)
        entry.update({
            "language": data.get("language") or detect_language(source_name) or DEFAULT_LANGUAGE,
            "year": data.get("year") or entry.get("year"),
            "_cache_key": key,
        })
        if _is_unknown_media(entry):
            logger.warning(f"[RESTORE] Preserving unknown media entry on disk and routing to review queue: {key}")
            _record_review_job(entry, confidence=0.0, reason="unknown_media_restore")
            continue
        restore_candidates.append(entry)

    for key in stale_cache_keys:
        FILE_ID_CACHE.pop(key, None)
    if stale_cache_keys:
        await save_cache_async()
        logger.info(f"[RESTORE] Removed {len(stale_cache_keys)} unidentified media cache record(s).")

    # One Jellyfin publication per canonical movie/episode. Telegram source
    # variants remain in the cache/catalog for playback and re-upload recovery,
    # but restoring all variants creates visible duplicates after every restart.
    winners = {}
    for entry in sorted(
        restore_candidates,
        key=lambda item: (
            0 if item.get("year") else 1,
            0 if "unknown" not in str(item.get("title", "")).lower() else 1,
            int(item.get("chat_id") or 0),
            int(item.get("msg_id") or 0),
        ),
    ):
        identity = _canonical_media_key(entry)
        winners.setdefault(identity, entry)

    # Count existing .strm files on disk across movies and shows
    existing_strm_count = 0
    for rdir in (MOVIES_DIR, SHOWS_DIR):
        if os.path.isdir(rdir):
            for r, _, fs in os.walk(rdir):
                existing_strm_count += sum(1 for f in fs if f.lower().endswith(".strm"))

    for entry in winners.values():
        record = _catalog_upsert(entry)
        if record and record.deleted:
            continue
        await create_strm_file_async(entry)
        if entry.get("target_dir") and entry.get("strm_base"):
            expected_paths.add(os.path.normpath(
                os.path.join(entry["target_dir"], f"{entry['strm_base']}.strm")
            ))
        count += 1

    if count > 0:
        logger.info(f"[RESTORE] Restored {count} .strm file(s) from persistent disk cache.")
        await trigger_jellyfin_scan()

    # Layout migration: an old flat-layout .strm (Movies/<Lang>/X.strm or
    # TV Shows/<Lang>/<Show>/Season NN/X.strm) duplicates a per-title folder one,
    # so Jellyfin shows both. Prune any .strm that points at our streamer but is
    # NOT the freshly-restored per-title path.
    pruned = await _prune_orphan_strms(movies_dir=MOVIES_DIR, shows_dir=SHOWS_DIR, keep=expected_paths)
    if pruned:
        logger.info(f"[RESTORE] Pruned {pruned} orphaned .strm file(s) from the old layout.")
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

    # Note: Library reconciliation and IMDb upgrade are cleanly scheduled via
    # _startup_reconcile() in start_background_tasks(app) to prevent duplicate runs.

_RE_ENRICH_RUNNING = False


async def reindex_all_with_imdb(force_all: bool = True) -> int:
    """Re-index all movies & TV media using official IMDb suggestion API (omkarcloud/imdb-scraper).
    Safely reconciles metadata, writes authentic NFOs, downloads Amazon CDN posters, routes untracked
    files and directories to the review queue without deleting, and forces item-level Jellyfin refreshes.
    """
    logger.info(f"[IMDB-REINDEX] Starting full IMDb re-indexing pass (force_all={force_all})...")
    # Step 1: Scan untracked stream pointers and directories on disk (route to review queue; clean genuinely empty folders)
    valid_dirs = set()
    valid_strms = set()
    for d in FILE_ID_CACHE.values():
        if isinstance(d, dict):
            if d.get("strm_path"):
                valid_strms.add(os.path.normpath(d["strm_path"]).lower())
            if d.get("target_dir"):
                valid_dirs.add(os.path.normpath(d["target_dir"]).lower())
            if d.get("show_root"):
                valid_dirs.add(os.path.normpath(d["show_root"]).lower())

    for rec in MEDIA_CATALOG.list_records(include_deleted=False):
        job = MEDIA_CATALOG.find_job(rec.source_chat_id, rec.source_message_id)
        if job and isinstance(job.payload, dict):
            p_strm = job.payload.get("strm_path")
            if p_strm:
                valid_strms.add(os.path.normpath(p_strm).lower())
            p_tdir = job.payload.get("target_dir")
            if p_tdir:
                valid_dirs.add(os.path.normpath(p_tdir).lower())
            p_sroot = job.payload.get("show_root")
            if p_sroot:
                valid_dirs.add(os.path.normpath(p_sroot).lower())

    # Only scan untracked files when valid catalog records exist (safety guard)
    if valid_strms:
        for root in (MOVIES_DIR, SHOWS_DIR):
            if not os.path.isdir(root):
                continue
            for dp, dns, fns in os.walk(root, topdown=False):
                for fn in fns:
                    if fn.lower().endswith(".strm"):
                        fp = os.path.normpath(os.path.join(dp, fn))
                        if fp.lower() not in valid_strms:
                            try:
                                with open(fp, "r", encoding="utf-8", errors="ignore") as _sf:
                                    _c = _sf.read(256)
                                if "/stream/" in _c or "127.0.0.1" in _c:
                                    logger.warning(f"[REINDEX] Preserving untracked .strm file and routing to review queue: {fp}")
                                    _record_review_job(
                                        {"strm_path": fp, "file_name": fn, "content_sample": _c[:128]},
                                        confidence=0.0,
                                        reason=f"untracked_strm_file:{fn}"
                                    )
                            except Exception:
                                pass
                norm_dp = os.path.normpath(dp)
                if norm_dp in (os.path.normpath(MOVIES_DIR), os.path.normpath(SHOWS_DIR)):
                    continue
                parent = os.path.dirname(norm_dp)
                if os.path.normpath(parent) in (os.path.normpath(MOVIES_DIR), os.path.normpath(SHOWS_DIR)):
                    continue
                remaining = os.listdir(dp)
                if not remaining:
                    try:
                        os.rmdir(dp)
                        logger.info(f"[CLEANUP] Removed empty directory: {dp}")
                    except OSError:
                        pass
                elif valid_dirs and norm_dp.lower() not in valid_dirs and not any(f.lower().endswith('.strm') for f in remaining):
                    # Directory contains files without tracked .strm media (whether sidecars, unknown formats, or user data)
                    # NEVER delete files; preserve and record review job
                    logger.warning(f"[REINDEX] Preserving untracked directory with files: {dp}")
                    _record_review_job(
                        {"directory": dp, "files": remaining[:10]},
                        confidence=0.0,
                        reason=f"untracked_directory:{os.path.basename(dp)}"
                    )

    # Step 2: Iterate through all cached media
    updated_count = 0
    refreshed_paths = []
    connector = aiohttp.TCPConnector(family=socket.AF_INET)
    async with aiohttp.ClientSession(connector=connector) as session:
        for key, data in list(FILE_ID_CACHE.items()):
            if not isinstance(data, dict):
                continue
            raw_name = data.get("file_name") or data.get("raw_name") or data.get("title") or ""
            raw_lower = raw_name.lower()
            parsed = parse_media_meta(raw_name)
            ext = extract_media_info(raw_name)
            is_tv = bool(ext.get("is_tv") or data.get("is_tv") or parsed.get("is_tv"))
            mtype = "tv" if is_tv else "movie"
            title = ext.get("title") or parsed.get("clean_title") or data.get("clean_title") or data.get("title") or ""
            year = ext.get("year") or parsed.get("year") or data.get("year")
            season = ext.get("season") or data.get("season") or 1
            episode = ext.get("episode") or data.get("episode") or 1
            lang = data.get("language") or parsed.get("language") or DEFAULT_LANGUAGE

            imdb_hit = await _fetch_imdb_suggestion(title, year, mtype)
            if not imdb_hit and year:
                imdb_hit = await _fetch_imdb_suggestion(title, None, mtype)
            if not imdb_hit and parsed.get("title") and parsed["title"] != title:
                imdb_hit = await _fetch_imdb_suggestion(parsed["title"], year, mtype)

            official_title = (imdb_hit.get("l") if imdb_hit else None) or title
            official_year = (imdb_hit.get("y") if imdb_hit else None) or year
            imdb_id = (imdb_hit.get("id") if imdb_hit else None) or data.get("imdb_id")
            poster_url = ((imdb_hit.get("i") or {}).get("imageUrl") if imdb_hit else None) or data.get("poster_url")

            # Fetch Cinemeta for background & plot
            c_meta = None
            if imdb_id:
                try:
                    c_meta = await _fetch_cinemeta_meta(imdb_id, mtype)
                except Exception:
                    pass
            backdrop_url = (c_meta.get("background") if c_meta else None) or data.get("backdrop_url")
            overview = (c_meta.get("description") if c_meta else None) or data.get("overview") or ""
            genres = (c_meta.get("genres") if c_meta else None) or []

            # Update cache data
            data["title"] = official_title
            data["clean_title"] = official_title
            data["language"] = lang
            if official_year:
                data["year"] = int(official_year)
            if imdb_id:
                data["imdb_id"] = str(imdb_id)
            if poster_url:
                data["poster_url"] = str(poster_url)
            if backdrop_url:
                data["backdrop_url"] = str(backdrop_url)

            old_target = data.get("target_dir")
            old_imdb = data.get("imdb_id")
            if is_tv:
                s_num = int(season or data.get("season") or 1)
                e_num = int(episode or data.get("episode") or 1)
                data["is_tv"] = True
                data["show_name"] = official_title
                data["season"] = s_num
                data["episode"] = e_num
                data["strm_base"] = strm_base(data)
                target_dir = strm_dir(MOVIES_DIR, SHOWS_DIR, data)
                show_root = tv_show_root(SHOWS_DIR, lang, official_title, official_year)
                data["show_root"] = show_root
                new_strm_base = data["strm_base"]
            else:
                data["is_tv"] = False
                data["strm_base"] = strm_base(data)
                target_dir = strm_dir(MOVIES_DIR, SHOWS_DIR, data)
                new_strm_base = data["strm_base"]

            data["strm_base"] = new_strm_base

            # If old_target exists and differs from target_dir, remove old_target safely
            norm_old = os.path.normpath(old_target) if old_target else ""
            norm_new = os.path.normpath(target_dir) if target_dir else ""
            if (
                norm_old
                and norm_old != norm_new
                and os.path.isdir(norm_old)
                and norm_old not in (os.path.normpath(MOVIES_DIR), os.path.normpath(SHOWS_DIR))
                and os.path.dirname(norm_old) not in (os.path.normpath(MOVIES_DIR), os.path.normpath(SHOWS_DIR))
            ):
                try:
                    if os.path.isdir(norm_old) and not os.listdir(norm_old):
                        os.rmdir(norm_old)
                        logger.info(f"[IMDB-REINDEX] Cleaned obsolete empty folder: {norm_old}")
                except Exception:
                    pass

            os.makedirs(target_dir, exist_ok=True)
            data["target_dir"] = target_dir
            new_strm_path = os.path.join(target_dir, f"{new_strm_base}.strm")
            data["strm_path"] = new_strm_path

            # Write genuine .strm file
            _chat, _msg = _split_cache_key(key)
            cid = data.get("chat_id") or _chat
            mid = data.get("msg_id") or _msg
            strm_content = _stream_url(cid, mid)
            with open(new_strm_path, "w", encoding="utf-8") as sf:
                sf.write(strm_content)

            # Write NFO file
            nfo_detail = {
                "title": official_title,
                "name": official_title,
                "original_title": official_title,
                "original_name": official_title,
                "release_date": f"{official_year}-01-01" if official_year else "",
                "first_air_date": f"{official_year}-01-01" if official_year else "",
                "year": official_year or "",
                "overview": overview,
                "imdb_id": imdb_id or "",
                "id": data.get("tmdb_id") or "",
                "genres": [{"name": g} if isinstance(g, str) else g for g in genres] if genres else [],
            }
            if is_tv:
                tv_nfo = os.path.join(show_root, "tvshow.nfo")
                with open(tv_nfo, "w", encoding="utf-8") as nf:
                    nf.write(build_tvshow_nfo(nfo_detail))
                ep_nfo = os.path.join(target_dir, f"{new_strm_base}.nfo")
                ep_detail = {
                    "season_number": s_num,
                    "episode_number": e_num,
                    "name": f"Episode {e_num}",
                    "overview": overview,
                    "id": data.get("tmdb_id") or "",
                }
                with open(ep_nfo, "w", encoding="utf-8") as nf:
                    nf.write(build_episode_nfo(ep_detail))
            else:
                for nfo_p in (os.path.join(target_dir, f"{new_strm_base}.nfo"), os.path.join(target_dir, "movie.nfo")):
                    with open(nfo_p, "w", encoding="utf-8") as nf:
                        nf.write(build_movie_nfo(nfo_detail))

            # Download genuine Amazon poster & backdrop
            art_dir = show_root if is_tv else target_dir
            if force_all or (old_imdb and imdb_id and old_imdb != imdb_id):
                for stale_art in ("poster.jpg", "folder.jpg", "thumb.jpg", "backdrop.jpg", "fanart.jpg", "landscape.jpg", "season01-poster.jpg", "season-all-poster.jpg"):
                    for check_dir in (art_dir, target_dir):
                        stale_p = os.path.join(check_dir, stale_art)
                        if os.path.exists(stale_p):
                            try:
                                os.remove(stale_p)
                            except OSError:
                                pass
            if poster_url:
                p_dst = os.path.join(art_dir, "poster.jpg")
                if force_all or not os.path.exists(p_dst) or os.path.getsize(p_dst) < 15000:
                    if await _download_image(session, poster_url, p_dst):
                        for copy_name in ("folder.jpg", "thumb.jpg"):
                            try:
                                with open(p_dst, "rb") as s, open(os.path.join(art_dir, copy_name), "wb") as d:
                                    d.write(s.read())
                            except Exception:
                                pass
                        if is_tv:
                            for copy_name in ("season01-poster.jpg", "season-all-poster.jpg"):
                                try:
                                    with open(p_dst, "rb") as s, open(os.path.join(show_root, copy_name), "wb") as d:
                                        d.write(s.read())
                                except Exception:
                                    pass
                            for copy_name in ("poster.jpg", "folder.jpg"):
                                try:
                                    with open(p_dst, "rb") as s, open(os.path.join(target_dir, copy_name), "wb") as d:
                                        d.write(s.read())
                                except Exception:
                                    pass
            if backdrop_url:
                b_dst = os.path.join(art_dir, "backdrop.jpg")
                if force_all or not os.path.exists(b_dst) or os.path.getsize(b_dst) < 15000:
                    if await _download_image(session, backdrop_url, b_dst):
                        for copy_name in ("fanart.jpg", "landscape.jpg"):
                            try:
                                with open(b_dst, "rb") as s, open(os.path.join(art_dir, copy_name), "wb") as d:
                                    d.write(s.read())
                            except Exception:
                                pass

            FILE_ID_CACHE[key] = data
            refreshed_paths.append(new_strm_path)
            updated_count += 1
            logger.info(f"[IMDB-REINDEX] Successfully re-indexed '{official_title}' ({official_year}) [{imdb_id}]")

        await save_cache_async()

    # Step 3: Deduplicate & purge stale items from Jellyfin SQLite DB
    await deduplicate_jellyfin_items()

    # Step 4: Batch trigger single Jellyfin scan after reindexing completes
    await request_jellyfin_scan()

    # Step 5: Refresh newly created / updated items with bounded concurrency
    async def _bounded_refreshes():
        sem = asyncio.Semaphore(4)
        async def _one(p):
            async with sem:
                await refresh_jellyfin_item_by_path(p, retries=2, delay=2.0)
        await asyncio.gather(*(_one(p) for p in refreshed_paths), return_exceptions=True)
    asyncio.create_task(_bounded_refreshes())

    logger.info(f"[IMDB-REINDEX] Completed full IMDb re-indexing of {updated_count} media items.")
    return updated_count


def migrate_double_year_folders(library_root: str):
    """Find and safely migrate legacy duplicate-year folders like 'Title (YYYY) (YYYY)'
    into canonical 'Title (YYYY)'. Moves files, normalizes filenames, updates FILE_ID_CACHE,
    and removes the old folder with os.rmdir without deleting media.
    """
    if not os.path.isdir(library_root):
        return
    cache_dirty = False
    for root, dirs, _ in os.walk(library_root, topdown=True):
        for d in list(dirs):
            match = re.search(r"^(.*?)\s*\((\d{4})\)\s*\(\2\)$", d)
            if not match:
                continue
            base_title = match.group(1).strip()
            year = match.group(2)
            canonical_name = f"{base_title} ({year})"
            old_dir = os.path.join(root, d)
            new_dir = os.path.join(root, canonical_name)
            logger.info(f"[MIGRATE-YEAR] Found double-year folder '{old_dir}' -> normalizing to '{new_dir}'")
            os.makedirs(new_dir, exist_ok=True)
            for sub_root, _, files in os.walk(old_dir):
                rel = os.path.relpath(sub_root, old_dir)
                target_sub = os.path.join(new_dir, rel) if rel != "." else new_dir
                os.makedirs(target_sub, exist_ok=True)
                for fn in files:
                    src_file = os.path.join(sub_root, fn)
                    fn_norm = re.sub(r"\s*\((\d{4})\)\s*\(\1\)", r" (\1)", fn)
                    dst_file = os.path.join(target_sub, fn_norm)
                    if not os.path.exists(dst_file):
                        try:
                            shutil.move(src_file, dst_file)
                            logger.info(f"[MIGRATE-YEAR] Moved '{src_file}' -> '{dst_file}'")
                        except Exception as e:
                            logger.warning(f"[MIGRATE-YEAR] Failed moving '{src_file}': {e}")
            for k, v in FILE_ID_CACHE.items():
                if not isinstance(v, dict):
                    continue
                changed = False
                td = v.get("target_dir")
                if td and os.path.normpath(td).startswith(os.path.normpath(old_dir)):
                    v["target_dir"] = os.path.normpath(td).replace(os.path.normpath(old_dir), os.path.normpath(new_dir), 1)
                    changed = True
                sp = v.get("strm_path")
                if sp and os.path.normpath(sp).startswith(os.path.normpath(old_dir)):
                    new_sp = os.path.normpath(sp).replace(os.path.normpath(old_dir), os.path.normpath(new_dir), 1)
                    new_sp = re.sub(r"\s*\((\d{4})\)\s*\(\1\)", r" (\1)", new_sp)
                    v["strm_path"] = new_sp
                    changed = True
                sr = v.get("show_root")
                if sr and os.path.normpath(sr).startswith(os.path.normpath(old_dir)):
                    v["show_root"] = os.path.normpath(sr).replace(os.path.normpath(old_dir), os.path.normpath(new_dir), 1)
                    changed = True
                if changed:
                    cache_dirty = True
            for sub_root, sub_dirs, _ in os.walk(old_dir, topdown=False):
                for sd in sub_dirs:
                    try:
                        os.rmdir(os.path.join(sub_root, sd))
                    except OSError:
                        pass
            try:
                os.rmdir(old_dir)
                logger.info(f"[MIGRATE-YEAR] Successfully removed empty old directory '{old_dir}'")
            except OSError:
                pass
    if cache_dirty:
        _save_cache_sync()


_RECONCILE_RUNNING = False


async def _reconcile_and_upgrade_library():
    """Startup reconciliation:
    1. Permanently remove TV Shows/English and blacklisted demo entries.
    2. Reset mis-matched entries in FILE_ID_CACHE (e.g. Indian film matched to Portuguese/European).
    3. Re-index un-indexed or outdated media with IMDb suggestion API and download authentic Amazon CDN posters.
    4. Re-enrich entries needing HD artwork.
    """
    global _RECONCILE_RUNNING
    if _RECONCILE_RUNNING:
        logger.info("[RECONCILE] Reconciliation already in progress; skipping duplicate call.")
        return
    _RECONCILE_RUNNING = True
    try:
        # Step -1: Migrate legacy duplicate-year folders (e.g. 'Ne Zha (2019) (2019)' -> 'Ne Zha (2019)')
        try:
            migrate_double_year_folders(MOVIES_DIR)
            migrate_double_year_folders(SHOWS_DIR)
        except Exception as _mig_exc:
            logger.warning(f"[RECONCILE] Double-year migration notice: {_mig_exc}")

        # Step 0: Run safe IMDb re-indexing pass (force_all=False prevents hammering during startup)
        try:
            await reindex_all_with_imdb(force_all=False)
        except Exception as _re_exc:
            logger.warning(f"[RECONCILE] IMDb re-indexing notice: {_re_exc}")


        # Step 1b: Merge duplicate TV show folders in SHOWS_DIR (e.g. 'Show' and 'Show (2024)')
        if os.path.isdir(SHOWS_DIR):
            try:
                subdirs = [d for d in os.listdir(SHOWS_DIR) if os.path.isdir(os.path.join(SHOWS_DIR, d))]
                grouped = {}
                for d in subdirs:
                    base = re.sub(r"\s*\(\d{4}\)$", "", d).strip().lower()
                    if base:
                        grouped.setdefault(base, []).append(d)

                for base, folders in grouped.items():
                    if len(folders) <= 1:
                        continue
                    folders_sorted = sorted(
                        folders,
                        key=lambda f: (1 if re.search(r"\(\d{4}\)", f) else 0, len(f)),
                        reverse=True,
                    )
                    canonical = folders_sorted[0]
                    canonical_path = os.path.join(SHOWS_DIR, canonical)
                    for dup in folders_sorted[1:]:
                        dup_path = os.path.join(SHOWS_DIR, dup)
                        logger.info(f"[MERGE-SHOWS] Merging duplicate TV show folder '{dup}' -> '{canonical}'")
                        for root, _, files in os.walk(dup_path):
                            rel = os.path.relpath(root, dup_path)
                            dest_dir = os.path.join(canonical_path, rel) if rel != "." else canonical_path
                            os.makedirs(dest_dir, exist_ok=True)
                            for f in files:
                                src_f = os.path.join(root, f)
                                dst_f = os.path.join(dest_dir, f)
                                if not os.path.exists(dst_f):
                                    try:
                                        shutil.move(src_f, dst_f)
                                    except Exception:
                                        pass
                        for root_dup, dirs_dup, _ in os.walk(dup_path, topdown=False):
                            for d in dirs_dup:
                                try:
                                    os.rmdir(os.path.join(root_dup, d))
                                except OSError:
                                    pass
                        try:
                            os.rmdir(dup_path)
                        except OSError:
                            pass
            except Exception as exc:
                logger.warning(f"[MERGE-SHOWS] Error merging duplicate TV folders: {exc}")

        # Step 1: Scan library roots and clean only genuinely empty directories with os.rmdir
        for root in (MOVIES_DIR, SHOWS_DIR):
            if not os.path.exists(root):
                continue
            for dirpath, dirnames, filenames in os.walk(root, topdown=False):
                if dirpath == root:
                    continue
                # Never delete media files or directories automatically based on blacklist strings
                for dn in dirnames:
                    full_dn = os.path.join(dirpath, dn)
                    if os.path.isdir(full_dn):
                        try:
                            # Only remove directory if it is genuinely empty (never recursive)
                            if not os.listdir(full_dn):
                                os.rmdir(full_dn)
                                logger.info(f"[RECONCILE] Removed empty directory: {dn}")
                        except OSError:
                            pass

        # Step 2: Quarantine blacklisted candidates into review queue without deleting files or directories
        for key, data in list(FILE_ID_CACHE.items()):
            if not isinstance(data, dict):
                continue
            name = data.get("file_name") or data.get("title") or ""
            strm_path = data.get("strm_path") or ""
            if is_blacklisted(name) or is_blacklisted(strm_path):
                logger.warning(f"[RECONCILE] Routing blacklisted candidate '{name}' to review queue (preserved on disk)")
                _record_review_job(data, confidence=0.0, reason=f"blacklisted_review:{name}")

        # Step 3: Upgrade artwork or re-enrich mis-matched entries
        to_enrich = []
        for key, data in list(FILE_ID_CACHE.items()):
            if not isinstance(data, dict):
                continue
            target_dir = data.get("target_dir") or (
                strm_dir(MOVIES_DIR, SHOWS_DIR, data)
                if ("title" in data or "show_name" in data)
                else None
            )
            if not target_dir or not os.path.exists(target_dir):
                continue

            backdrop = os.path.join(target_dir, "backdrop.jpg")
            thumb = os.path.join(target_dir, "thumb.jpg")
            landscape = os.path.join(target_dir, "landscape.jpg")
            fanart = os.path.join(target_dir, "fanart.jpg")
            logo = os.path.join(target_dir, "logo.png")
            poster = os.path.join(target_dir, "poster.jpg")

            # If the movie already has a valid TMDB ID and valid artwork, mirror to thumb/folder and DO NOT re-enrich!
            has_valid_poster = os.path.exists(poster) and os.path.getsize(poster) >= 15000
            has_valid_backdrop = os.path.exists(backdrop) and os.path.getsize(backdrop) >= 15000

            if has_valid_backdrop:
                for extra in (fanart, landscape, thumb):
                    if not os.path.exists(extra):
                        try:
                            with open(backdrop, "rb") as s, open(extra, "wb") as d:
                                d.write(s.read())
                        except OSError:
                            pass
            if has_valid_poster:
                folder_jpg = os.path.join(target_dir, "folder.jpg")
                if not os.path.exists(folder_jpg):
                    try:
                        with open(poster, "rb") as s, open(folder_jpg, "wb") as d:
                            d.write(s.read())
                    except OSError:
                        pass

            # Only enrich if truly missing TMDB identity or completely missing poster/backdrop
            needs_hd = (
                not data.get("tmdb_id")
                or not has_valid_poster
                or not has_valid_backdrop
            )

            # Check if this item had a suspicious match or mismatched NFO on disk
            lang = (data.get("language") or "").lower()
            tmdb_title = (data.get("tmdb_title") or "").lower()
            tmdb_year = data.get("tmdb_year")
            file_year = data.get("year")
            nfo_file = os.path.join(target_dir, f"{data.get('strm_base', strm_base(data))}.nfo")
            movie_nfo = os.path.join(target_dir, "movie.nfo")
            nfo_text = ""
            for nf in (nfo_file, movie_nfo):
                if os.path.exists(nf):
                    try:
                        with open(nf, "r", encoding="utf-8", errors="ignore") as fh:
                            nfo_text = fh.read(2048).lower()
                            break
                    except OSError:
                        pass

            suspicious = False
            raw_name_lower = (data.get("file_name") or data.get("title") or "").lower()
            if not suspicious and (data.get("tmdb_id") == 210879 or "generation iron" in tmdb_title or "generation iron" in nfo_text or "210879" in nfo_text):
                suspicious = True
            if not suspicious and "obsession" in raw_name_lower:
                if data.get("tmdb_id") != 1339713:
                    suspicious = True
            if not suspicious and "aadu" in raw_name_lower and ("3" in raw_name_lower or "part" in raw_name_lower):
                if data.get("tmdb_id") in (324976, 489709) or "aadu 2" in tmdb_title or tmdb_title == "aadu":
                    suspicious = True
            if not suspicious and lang in ("tamil", "malayalam", "telugu", "hindi", "kannada"):
                for foreign_pfx in ("a vingança", "vingança", "el ", "die ", "der "):
                    if tmdb_title.startswith(foreign_pfx):
                        suspicious = True
                        break
                if not suspicious and "patriot" in raw_name_lower:
                    if data.get("tmdb_id") == 2108 or "mel gibson" in nfo_text or (tmdb_year and tmdb_year <= 2010):
                        suspicious = True
                if not suspicious and file_year and tmdb_year and abs(int(file_year) - int(tmdb_year)) >= 4:
                    suspicious = True
            if not suspicious and "khalifa" in raw_name_lower:
                if (data.get("tmdb_id") != 1036081) or "queer" in tmdb_title or (tmdb_year and tmdb_year != 2026):
                    suspicious = True
            if not suspicious and "paradise" in raw_name_lower and ("2026" in raw_name_lower or file_year == 2026):
                if "siege" in tmdb_title or (data.get("tmdb_id") != 1145851):
                    suspicious = True
            if not suspicious and "the money" in tmdb_title and (tmdb_year and tmdb_year <= 1980):
                suspicious = True

            entry = dict(data)
            entry["_cache_key"] = key
            raw_cand = data.get("file_name") or data.get("raw_name") or data.get("clean_title") or data.get("title") or ""
            entry["search_candidates"] = _search_candidates(raw_cand)

            if suspicious:
                logger.info(f"[RECONCILE] Quarantining mismatched/suspicious metadata for '{entry.get('title')}'")
                entry.pop("tmdb_id", None)
                entry.pop("tmdb_title", None)
                entry.pop("tmdb_year", None)
                data.pop("tmdb_id", None)
                data.pop("tmdb_title", None)
                data.pop("tmdb_year", None)
                # Clear bad metadata sidecars non-destructively, preserving all .strm media files
                if target_dir and os.path.isdir(target_dir):
                    for wrong_file in ("movie.nfo", "poster.jpg", "folder.jpg", "backdrop.jpg", "fanart.jpg", "landscape.jpg", "thumb.jpg", "logo.png", "clearlogo.png"):
                        wf = os.path.join(target_dir, wrong_file)
                        if os.path.exists(wf):
                            try:
                                os.remove(wf)
                            except OSError:
                                pass
                if os.path.exists(nfo_file):
                    try:
                        os.remove(nfo_file)
                    except OSError:
                        pass
                # Quarantine in review queue: preserves media asset while awaiting manual verification
                _record_review_job(entry, confidence=0.0, reason=f"reconcile_suspicious_metadata:{entry.get('title')}")
                to_enrich.append(entry)
            elif needs_hd:
                to_enrich.append(entry)

        if to_enrich:
            logger.info(f"[ARTWORK-UPGRADE] Upgrading artwork for {len(to_enrich)} item(s)...")
            for entry in to_enrich:
                try:
                    await enrich_media_metadata(entry)
                except Exception as exc:
                    logger.warning(f"[ARTWORK-UPGRADE] Error for '{entry.get('title')}': {exc}")
                await asyncio.sleep(0.3)
            await request_jellyfin_scan()

    except Exception as e:
        logger.warning(f"[UPGRADE] Library reconciliation noticed: {e}")
    finally:
        _RECONCILE_RUNNING = False


def _unresolved_enrich_entries():
    """Cache entries lacking any TMDB/AI resolution or carrying mismatched metadata, worth one more match pass."""
    entries = []
    for key, data in FILE_ID_CACHE.items():
        if not isinstance(data, dict):
            continue
        is_mismatched = False
        raw_check = (data.get("file_name") or data.get("title") or "").lower()
        if "iron" in raw_check and "man" in raw_check:
            if data.get("tmdb_id") == 210879 or "generation iron" in (data.get("tmdb_title") or "").lower():
                is_mismatched = True
        if not is_mismatched and (data.get("tmdb_id") or data.get("tmdb_title")):
            continue
        if not (data.get("file_id") and (data.get("file_name") or data.get("title"))):
            continue
        _chat_from_key, msg_id = _split_cache_key(key)
        chat_id = data.get("chat_id") or _chat_from_key
        base_name = data.get("file_name") or data.get("title") or f"Media_{msg_id}"
        entry = _meta_to_entry(
            msg_id, chat_id, data.get("file_id"), data.get("file_size") or 0, base_name
        )
        language = data.get("language") or detect_language(base_name) or DEFAULT_LANGUAGE
        year = data.get("year") or entry.get("year")
        entry.update({
            "language": language,
            "year": year,
            "_cache_key": key,
        })
        if _is_unknown_media(entry):
            continue
        if entry.get("is_tv"):
            entry["show_root"] = tv_show_root(
                SHOWS_DIR, language, entry.get("show_name") or entry.get("title"), year
            )
        entry["target_dir"] = strm_dir(MOVIES_DIR, SHOWS_DIR, entry)
        entry["strm_base"] = strm_base(entry)
        entries.append(entry)
        if len(entries) >= 60:
            break
    return entries


def _insufficient_art_entries():
    """Cache entries whose local poster.jpg is missing or below HD; a re-enrich
    refreshes artwork at full resolution (or marks it best-effort)."""
    entries = []
    for key, data in FILE_ID_CACHE.items():
        if not isinstance(data, dict):
            continue
        if not (data.get("file_id") and (data.get("file_name") or data.get("title"))):
            continue
        _chat_from_key, msg_id = _split_cache_key(key)
        chat_id = data.get("chat_id") or _chat_from_key
        base_name = data.get("file_name") or data.get("title") or f"Media_{msg_id}"
        entry = _meta_to_entry(
            msg_id, chat_id, data.get("file_id"), data.get("file_size") or 0, base_name
        )
        language = data.get("language") or detect_language(base_name) or DEFAULT_LANGUAGE
        year = data.get("year") or entry.get("year")
        entry.update({
            "language": language,
            "year": year,
            "_cache_key": key,
        })
        if _is_unknown_media(entry):
            continue
        if entry.get("is_tv"):
            entry["show_root"] = tv_show_root(
                SHOWS_DIR, language, entry.get("show_name") or entry.get("title"), year
            )
        entry["target_dir"] = strm_dir(MOVIES_DIR, SHOWS_DIR, entry)
        entry["strm_base"] = strm_base(entry)
        art_folder = entry.get("show_root") or entry.get("target_dir") or ""
        if art_folder and not _poster_art_ok(art_folder):
            entries.append(entry)
            if len(entries) >= 60:
                break
    return entries


async def _re_enrich_unresolved(entries):
    """Re-attempt TMDB metadata for entries that previously never resolved."""
    global _RE_ENRICH_RUNNING
    if _RE_ENRICH_RUNNING or not entries:
        return
    _RE_ENRICH_RUNNING = True
    try:
        logger.info(f"[TMDB-RESYNC] Re-attempting metadata for {len(entries)} unresolved entrie(s)...")
        for entry in entries:
            try:
                await enrich_media_metadata(entry)
            except Exception as exc:
                logger.warning(f"[TMDB-RESYNC] {entry.get('title')}: {exc}")
            await asyncio.sleep(0.25)
    finally:
        _RE_ENRICH_RUNNING = False

async def _client_dc_id(client):
    """Primary DC for a connected Pyrogram client, or None."""
    try:
        storage = getattr(client, "storage", None)
        if storage is None:
            return None
        dc_id = await storage.dc_id()
        return int(dc_id) if dc_id else None
    except Exception as e:
        logger.warning(f"[POOL] primary dc unavailable for {type(client).__name__}: {type(e).__name__}: {e}")
        return None


async def _register_main_client():
    """Register the primary client in the pool once it is connected."""
    if tg_app is None:
        return
    clients = apex_pool.snapshot()["clients"]
    if not any(c["id"] == 1 for c in clients):
        dc_id = await _client_dc_id(tg_app)
        await apex_pool.register(1, tg_app, ready=True, dc_id=dc_id)
        _install_media_transport(tg_app)
        logger.info(f"[POOL] primary client registered (dc={dc_id})")


async def _start_extra_clients():
    """Start extra streaming bots and register them in the pool."""
    for idx, client in extra_clients:
        try:
            if client.is_connected:
                continue
            await client.start()
            logger.info(f"[PYROGRAM] extra client {idx} started")
            dc_id = await _client_dc_id(client)
            await apex_pool.register(idx, client, ready=True, dc_id=dc_id)
            _install_media_transport(client)
            logger.info(f"[POOL] extra client {idx} registered (dc={dc_id})")
        except Exception as e:
            logger.warning(f"[PYROGRAM] extra client {idx} failed to start: {type(e).__name__}: {e}")
            try:
                await client.stop()
            except Exception:
                pass
        if APEX_CFG.extra_start_delay_seconds > 0:
            await asyncio.sleep(APEX_CFG.extra_start_delay_seconds)


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
            if "tg_user" in globals() and tg_user and not getattr(tg_user, "is_connected", False):
                try:
                    await tg_user.start()
                    user_me = await tg_user.get_me()
                    logger.info(f"[PYROGRAM] Pyrogram User Account client started! (id={getattr(user_me, 'id', '?')})")
                except Exception as ue:
                    logger.warning(f"[PYROGRAM] User Account client failed to start: {ue}")
            await restore_cached_strm_files()
            webhook_ok, webhook_url, webhook_detail = await register_telegram_webhook()
            if webhook_ok:
                logger.info(f"[WEBHOOK] Primary bot update delivery active: {webhook_url}")
            else:
                logger.warning(
                    f"[WEBHOOK] Primary bot webhook is not active: {webhook_detail}. "
                    f"Use /webhook-link to activate it from a browser."
                )
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
    if "tg_user" in globals() and tg_user and getattr(tg_user, "is_connected", False):
        try:
            await tg_user.stop()
        except Exception as e:
            logger.warning(f"[PYROGRAM] shutdown user client: {e}")

async def start_background_tasks(app):
    async def _reconciliation_loop():
        while True:
            try:
                await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)
                await deduplicate_jellyfin_items()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.info(f"[RECONCILE] Scheduled cleanup deferred: {type(exc).__name__}")

    async def _guard():
        try:
            await start_pyrogram()
        except Exception as e:
            logger.exception(f"[PYROGRAM] start_pyrogram crashed: {e}")

    async def _delayed_subtitle_backfill():
        # Allow Jellyfin to complete initial startup before running backfill
        await asyncio.sleep(25)
        try:
            await backfill_missing_subtitles()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"[SUBTITLES] Startup backfill notice: {exc}")

    async def _startup_reconcile():
        await asyncio.sleep(5)
        try:
            logger.info("[RECONCILE] Running startup library reconciliation & IMDb upgrade...")
            await _reconcile_and_upgrade_library()
        except Exception as exc:
            logger.warning(f"[RECONCILE] Startup reconciliation notice: {exc}")

    app['pyrogram_task'] = asyncio.create_task(_guard())
    app['reconciliation_task'] = asyncio.create_task(_reconciliation_loop())
    app['channel_sync_task'] = asyncio.create_task(_configured_channel_sync_loop())
    app['subtitle_backfill_task'] = asyncio.create_task(_delayed_subtitle_backfill())
    app['startup_reconcile_task'] = asyncio.create_task(_startup_reconcile())

async def cleanup_background_tasks(app):
    for task_name in ("subtitle_backfill_task", "reconciliation_task", "channel_sync_task", "startup_reconcile_task"):
        task = app.get(task_name)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    await stop_pyrogram()

app = web.Application()
app.add_routes(routes)
app.on_startup.append(start_background_tasks)
app.on_cleanup.append(cleanup_background_tasks)

if __name__ == "__main__":
    logger.info(f"Starting TG-Drive High-Speed 5G Stream Proxy on {HOST}:{PORT}")
    web.run_app(app, host=HOST, port=PORT)
