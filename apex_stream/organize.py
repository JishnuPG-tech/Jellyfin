"""Language detection + Jellyfin path organization.

Pure helpers (no Telegram dependency) that decide where a piece of media
lands on disk and how subtitles are matched/renamed:

- `detect_language(text)`   -> canonical language name (e.g. "Malayalam")
- `language_code(name)`     -> ISO 639-2/B subtitle code (e.g. "mal")
- `movie_target_dir(...)`   -> Movies/<Language>
- `tv_target_dir(...)`      -> TV Shows/<Language>/<Show>/Season NN
- `match_subtitle_media()`  -> best FILE_ID_CACHE entry for a subtitle base

Default language ("English") is used when no explicit tag is found, so plain
English-release filenames like "SpiderMan Homecoming 2017 720p x264" land in
Movies/English without requiring a tag.
"""

import os
import re
from typing import Dict, List, Optional, Tuple

DEFAULT_LANGUAGE = "English"

SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".sbv"}

# (display name, iso-639-2b subtitle code, [detection tokens])
# Ordered by detection priority: distinctive regional languages first so a
# "Hindi Web-DL" file is not mis-classified by a later generic hyphen token.
_LANGUAGES: List[Tuple[str, str, List[str]]] = [
    ("Malayalam", "mal", ["malayalam", "mal"]),
    ("Hindi", "hin", ["hindi", "hin"]),
    ("Tamil", "tam", ["tamil", "tam"]),
    ("Telugu", "tel", ["telugu", "tel"]),
    ("Kannada", "kan", ["kannada", "kan"]),
    ("Bengali", "ben", ["bengali", "bangla"]),
    ("Punjabi", "pan", ["punjabi"]),
    ("Marathi", "mar", ["marathi"]),
    ("Gujarati", "guj", ["gujarati", "guj"]),
    ("Urdu", "urdu", ["urdu"]),
    ("English", "eng", ["english", "eng"]),
    ("Korean", "kor", ["korean", "kor"]),
    ("Japanese", "jp", ["japanese", "jpn", "jap"]),
    ("Chinese", "chi", ["chinese", "chi", "mandarin"]),
    ("Spanish", "spa", ["spanish", "spa", "espanol", "castellano"]),
    ("French", "fra", ["french", "fra", "francais"]),
    ("German", "ger", ["german", "ger", "deutsch"]),
    ("Russian", "rus", ["russian", "rus"]),
    ("Arabic", "ara", ["arabic", "ara"]),
    ("Portuguese", "por", ["portuguese", "por", "brazilian"]),
    ("Indonesian", "ind", ["indonesian", "ind"]),
    ("Italian", "ita", ["italian", "ita"]),
]

_BY_NAME: Dict[str, str] = {name: code for name, code, _ in _LANGUAGES}

_ALL_LANG_TOKENS = {
    t.lower() for _, _, tokens in _LANGUAGES for t in tokens
}

# Generic release-quality tokens that add no matching value.
_STOP_TOKENS = {
    "bluray", "blu", "ray", "web", "webdl", "webrip", "bdrip", "dvdr", "dvd",
    "hdtv", "hdtvrip", "h264", "h265", "x264", "x265", "hevc", "aac", "ac3",
    "dd5", "dd5", "5.1", "7.1", "atmos", "truehd", "dts", "hdrip", "hdr10",
    "10bit", "8bit", "avc", "xvid", "divx", "wmv", "flac", "mp3", "dual",
    "audio", "multi", "subs", "proper", "repack", "extended", "uncut",
    "unrated", "remux", "2160p", "1440p", "1080p", "720p", "480p", "360p",
}


def _token_re(token: str) -> "re.Pattern[str]":
    return re.compile(
        r"(?<![a-z0-9])" + re.escape(token.lower()) + r"(?![a-z0-9])"
    )


def detect_language(text: Optional[str]) -> str:
    """Return the canonical language name detected in a filename/caption."""
    if not text:
        return DEFAULT_LANGUAGE
    lowered = text.lower()
    for name, _code, tokens in _LANGUAGES:
        for tok in tokens:
            if _token_re(tok).search(lowered):
                return name
    return DEFAULT_LANGUAGE


def language_code(language_name: str) -> str:
    """ISO 639-2/B subtitle-file code for a language name ('' if unknown)."""
    return _BY_NAME.get(language_name, "")


def safe_folder(name: str) -> str:
    """Folders/Jellyfin-safe version of a title or language name."""
    if not name:
        return "Unknown"
    cleaned = re.sub(r"[^\w .'(),\-!]+", "", name, flags=re.UNICODE).strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .") if cleaned else "Unknown"
    return cleaned[:120] or "Unknown"


def movie_target_dir(movies_dir: str, language: str = DEFAULT_LANGUAGE) -> str:
    return os.path.join(movies_dir, safe_folder(language))


def tv_target_dir(
    shows_dir: str,
    language: str = DEFAULT_LANGUAGE,
    show_name: Optional[str] = None,
    season: Optional[int] = None,
) -> str:
    show = safe_folder(show_name) if show_name else "Unknown_Show"
    season_num = int(season) if season else 1
    return os.path.join(
        shows_dir, safe_folder(language), show, f"Season {season_num:02d}"
    )


def subtitle_lang_hint(base_name: str) -> str:
    """Extract a language hint from dotted segments of a subtitle base name.

    e.g. "Movie.eng", "Show - S01E01.hin", "[Malayalam] Movie" -> language name.
    """
    for part in re.split(r"[.\-\[\]()\s_]+", base_name):
        part = part.strip().lower()
        if not part:
            continue
        for name, code, tokens in _LANGUAGES:
            if part in {code} or part in {t.lower() for t in tokens}:
                return name
    return DEFAULT_LANGUAGE


def subtitle_is_document(file_name: Optional[str], mime_type: Optional[str] = None) -> bool:
    if not file_name:
        return False
    ext = os.path.splitext(file_name)[1].lower()
    if ext in SUBTITLE_EXTS:
        return True
    if mime_type:
        mt = mime_type.lower()
        if any(s in mt for s in ("subrip", "webvtt", "x-subrip", "text/plain", "text/vtt")):
            return True
    return False


def _sig_tokens(text: str) -> set:
    toks = {t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOP_TOKENS}
    return {t for t in toks if not (t.isdigit() and len(t) == 4)}  # drop years


def match_subtitle_media(subtitle_base: str, entries: Dict, threshold: float = 0.6):
    """Return (msg_id, entry, score) of the best matching indexed media.

    `entries` is the FILE_ID_CACHE dict {str(msg_id): entry}. Matching is
    token-overlap based, tolerating release tags / season markers. None if no
    entry clears `threshold`.
    """
    # Drop language-tag tokens (e.g. "mal", "eng" in "Movie.mal.srt") so they
    # do not inflate the denominator of the subtitle side of the score.
    sub_toks = {t for t in _sig_tokens(subtitle_base) if t not in _ALL_LANG_TOKENS}
    if not sub_toks:
        return None
    best: Optional[Tuple[str, dict, float]] = None
    for msg_id, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        title_toks = _sig_tokens(entry.get("title") or "")
        if not title_toks:
            continue
        overlap = len(sub_toks & title_toks)
        score = overlap / len(sub_toks)
        if score > threshold and (best is None or score > best[2]):
            best = (msg_id, entry, score)
    return best