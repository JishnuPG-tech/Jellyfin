"""TMDB-driven metadata & library organization.

Pure, Telegram-free helpers (unit-testable without a network):

- `parse_media_meta`  -> robust movie/TV filename & caption parsing (year, season,
                         episode, title/query cleaning across many release schemes)
- `best_tmdb_match`   -> pick the best TMDB search hit for a query + year + type
- NFO builders        -> Kodi/Jellyfin-compatible `movie.nfo`, `tvshow.nfo`,
                         `episodedetails.nfo` XML built from TMDB JSON

The async aiohttp calls live in tg_streamer.py; everything here is pure.
"""

import os
import re
import unicodedata
from typing import Dict, List, Optional

from xml.sax.saxutils import escape as xml_escape

from .organize import (
    _ALL_LANG_TOKENS,
    _STOP_TOKENS,
    DEFAULT_LANGUAGE,
    detect_language,
    safe_folder,
    tmdb_language_code,
)

TPL_BASE = "https://image.tmdb.org/t/p"

YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")

# Radarr-style embedded IMDb ids: "Movie.tt12345678", "{imdb-tt1234567}", "[tt1234567]".
_IMDB_ID_RE = re.compile(r"(?i)\b(?:imdb[-_. ]?)?(tt\d{5,11})\b")

# Roman-numeral suffix/parts used by sequels ("Rocky III", "Part IV") that TMDB
# stores in Arabic form. Mapped before token matching so "III" == "3".
_ROMAN_TO_ARABIC = {
    "i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6",
    "vii": "7", "viii": "8", "ix": "9", "x": "10", "xi": "11", "xii": "12",
    "xiii": "13", "xiv": "14", "xv": "15", "xvi": "16", "xvii": "17",
    "xviii": "18", "xix": "19", "xx": "20", "xxi": "21", "xxii": "22",
    "xxiii": "23", "xxiv": "24", "xxv": "25", "xxvi": "26", "xxvii": "27",
    "xxviii": "28", "xxix": "29", "xxx": "30", "xl": "40", "l": "50",
    "lx": "60", "lxx": "70", "lxxx": "80", "xc": "90", "c": "100",
}


def extract_imdb_id(text: Optional[str]) -> Optional[str]:
    """Return the IMDb id embedded in a file name/caption, or None.

    Radarr/Jellyfin-style releases frequently carry the id verbatim
    ("Movie.{imdb-tt12345678}.720p", "[tt1234567]", "imdb-tt..."). When present
    it resolves deterministically through TMDB /find instead of fuzzy
    text search, removing every transliteration/spacing ambiguity.
    """
    if not text:
        return None
    m = _IMDB_ID_RE.search(text)
    return m.group(1).lower() if m else None


def _norm_tok(tok: str) -> str:
    """Normalize a token for matching: roman numeral -> arabic, else lowercase."""
    low = tok.lower()
    if len(low) <= 5 and low in _ROMAN_TO_ARABIC:
        return _ROMAN_TO_ARABIC[low]
    return low


_NFO_BAD_TITLE_RE = re.compile(
    r"<\s*(?:originaltitle|original_name)\s*>[^<]*\?{2,}[^<]*<", re.IGNORECASE
)
_NFO_FOREIGN_PUDDLE_RE = re.compile(
    r"(?i)<\s*(?:title|originaltitle|original_name)\s*>[^<]*(?:vingan[cç]a|a\s+vingan[cç]a)[^<]*<"
)


def nfo_needs_repair(nfo_path) -> bool:
    """True when an existing NFO's title looks malformed, has a mismatched foreign
    title (e.g. Portuguese vingan[cç]a), or is missing a valid TMDB id, so
    enrichment re-fetches and rewrites it."""
    if not nfo_path or not os.path.isfile(nfo_path):
        return False
    try:
        with open(nfo_path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read(0x4000)
    except OSError:
        return False
    if _NFO_BAD_TITLE_RE.search(text) or _NFO_FOREIGN_PUDDLE_RE.search(text):
        return True
    if "<tmdbid></tmdbid>" in text or "<tmdbid/>" in text or "<tmdbid>0</tmdbid>" in text:
        return True
    # Mismatched Generation Iron on Iron Man releases
    lower_path = str(nfo_path).lower()
    if "iron" in lower_path and "man" in lower_path:
        if "generation iron" in text.lower() or "<tmdbid>210879</tmdbid>" in text:
            return True
    return False

# TV patterns, in priority order. All return (show_name, season, episode) or None.
_SX_E = re.compile(r"(?i)\bS(\d{1,2})\s*(?:\.|\s|_|-)+\s*E(\d{1,3})\b")
_SX_EX = re.compile(r"(?i)\bS(\d{1,2})\s*E(\d{1,3})\b")
_SEASON_EP = re.compile(
    r"(?i)\bSeason\s*(\d{1,2})\s*(?:Episode\s*|Ep\s*)?(\d{1,3})\b"
)
_1X = re.compile(r"(?<!\d)(\d{1,2})x(\d{1,3})(?!\d)")
_EP_ONLY = re.compile(r"(?i)\b(?:E|Ep)\.?\s*(\d{1,3})\b")
_EPISODE_WORD = re.compile(r"(?i)\bEpisode\s*(\d{1,3})\b")

# Extra release/hardware tokens that pollute titles and queries.
_EXTRA_STOP = {
    "hdr", "dolby", "vision", "hd", "fhd", "uhd", "amzn", "nf", "netflix",
    "web-dl", "webdl", "webrip", "bdrip", "hdrip", "brrip", "hdtv", "remux",
    "10bit", "8bit", "50fps", "60fps", "multisub", "truehd", "ddplus", "ddp",
    "ddp5", "dtsx", "dtshd", "avc", "h264", "h265", "x264", "x265", "hevc",
    "itunes", "max", "disney", "disneyplus", "hulu", "paramount",
    "cmc", "hbo", "hbomax", "amazon", "googleplay", "sd", "hq",
    "ts", "cam", "hdcam", "blu-ray", "bluray", "hdrip", "dvdrip", "dvd",
    "webm", "mkv", "mp4",
    # OTT platform brandings that commonly prefix release audio/codec tags.
    "jiohotstar", "hotstar", "jiocinema", "sonyliv", "zee5", "mxplayer",
    "voot", "airtel", "sunnxt", "manoramamax", "primevideo", "nuefliks",
    "tvf", "mmax",
}

_ALL_STOP = _STOP_TOKENS | _EXTRA_STOP | _ALL_LANG_TOKENS

# Dotted release/quality anchors: when a trailing dotted segment follows one of
# these, it is almost always an uploader group tag (".x264.Pah", ".WEB-DL.XYZ")
# and safe to drop — genuine dotted titles like "K.G.F.Chapter.2" contain none.
_QUALITY_ANCHORS = {
    "720p", "1080p", "2160p", "1440p", "480p", "360p",
    "x264", "x265", "h264", "h265", "hevc", "avc", "aac", "ac3",
    "web-dl", "webdl", "webrip", "hdrip", "bdrip", "brrip", "hdtv",
    "remux", "bluray", "blu-ray", "10bit", "8bit", "truehd", "dts",
    "ddp5", "tvrip", "dvdrip", "dvd", "web", "amzn", "nf", "netflix",
    "mkv", "mp4", "avi", "mov", "ts", "m2ts", "mmax",
}


def _drop_dotted_group_tail(text: Optional[str]) -> str:
    """Drop a trailing dotted release-group (".x264.Pah" -> ".x264").

    Preserves genuine dotted title segments ("K.G.F.Chapter.2.2022") because
    those contain no release/quality anchor; only content AFTER the last
    recognized anchor segment is treated as group residue. Keeps tails that
    carry a year or a language/release token (real subtitle-less variants).
    """
    if not text:
        return text or ""
    stripped = text.strip()
    # Strip a real media extension so ".mkv" is never mistaken for a group,
    # but keep ".Pah"-style tails (not real extensions) so they can be detected.
    ext_re = re.split(r"(?i)\.(?:mkv|mp4|avi|mov|m4v|webm|ts|m2ts|mpg|mpeg|wmv|flv|iso)\s*$", stripped)
    base = ext_re[0]
    if "." not in base:
        return text
    parts = base.split(".")
    last_anchor = None
    for i, part in enumerate(parts):
        if part.strip().lower() in _QUALITY_ANCHORS:
            last_anchor = i
    if last_anchor is None:
        return text
    tail = parts[last_anchor + 1:]
    if not tail:
        return text
    tail_text = " ".join(p.strip() for p in tail)
    if re.search(r"\b(?:19|20)\d{2}\b", tail_text):
        return text
    # Keep a language token so detect_language still sees it after the drop
    # (e.g. ".1080p.Kannada"); pure codec/group residue is always dropped.
    if _ALL_LANG_TOKENS & set(re.findall(r"[a-z0-9]+", tail_text.lower())):
        return text
    return ".".join(parts[: last_anchor + 1])


def _sig_tokens(text: str) -> set:
    if not text:
        return set()
    cleaned_input = re.sub(r"(?i)\bspiderman\b", "spider man", text)
    norm_text = unicodedata.normalize('NFKD', cleaned_input).encode('ascii', 'ignore').decode('ascii')
    toks = {
        _norm_tok(t)
        for t in re.findall(r"[a-z0-9]+", norm_text.lower())
        if t not in _ALL_STOP
    }
    if not toks:
        toks = {
            _norm_tok(t)
            for t in re.findall(r"\w+", cleaned_input.lower())
            if t not in _ALL_STOP
        }
    # A lone 4-digit token is a numeric title (e.g. the film "2018"), not a year.
    if len(toks) == 1 and next(iter(toks)).isdigit() and len(next(iter(toks))) == 4:
        return toks
    return {t for t in toks if not (t.isdigit() and len(t) == 4)}


def _clean_check(text: str) -> str:
    """Lowercased token check description used for year/tag stripping."""
    return text


_MEDIA_EXT_RE = re.compile(
    r"(?i)\.(?:mkv|mp4|avi|mov|m4v|webm|ts|m2ts|mpg|mpeg|wmv|flv|iso|rar|zip|7z)\s*$"
)

_SIZE_RE = re.compile(
    r"(?i)\b\d+(?:\.\d+)?\s*(?:mb|gb|kib|mib|gib|tb)\b"
)


_URL_NOISE_RE = re.compile(
    r"(?i)\b(?:https?://\S+|www\.[a-z0-9\-]+(?:\.[a-z0-9\-]+)+|(?:t\.me|telegram\.me)/\S+|[a-z0-9\-]{2,}\.(?:com|org|net|vip|xyz|top|site|club|online|store|link|tv|pro|info))\b"
)
_WATERMARK_PAREN_RE = re.compile(
    r"(?i)\s*\([^)]*(?:www\.|t\.me|https?://|\.com|\.org|\.net|\.vip|\.in|\.xyz|tamil|bolly|cinema|esub|dual[- ]?audio|clean[- ]?audio|hq[- ]?line|rip)[^)]*\)"
)


def _strip_noise(text: str) -> str:
    """Strip group tags ([...], @Handle), the real media extension, file sizes and collapse
    whitespace.

    The media extension must be removed BEFORE dots are turned into spaces, or
    ".mkv" becomes a trailing "mkv" token that breaks the audio/language tail
    detection for every release that ends in one. Dots are release separators
    in filenames ("Movie.2021.720p.Pah") so they are turned into spaces here;
    keeping them produces dotted folder names and breaks both Jellyfin
    matching and TMDB search queries.
    """
    t = _SIZE_RE.sub(" ", text)
    t = _URL_NOISE_RE.sub(" ", t)
    t = _WATERMARK_PAREN_RE.sub(" ", t)
    t = re.sub(r"\[[^\]]*\]", " ", t)
    t = re.sub(r"@[\w.\-]+", " ", t)
    t = _MEDIA_EXT_RE.sub(" ", t)
    # "+" and "&" are release/present separators too ("Dd+5.1 H" -> "Dd 5 1 H",
    # "Birds of Prey & Fantabulous" -> "Birds of Prey  Fantabulous"). A dash is a
    # word separator ("DTS-MA", "Dead-Reckoning") so it is left as-is; Dots are
    # release separators in filenames ("Movie.2021.720p.Pah") so they are also
    # turned into spaces here; keeping them produces dotted folder names and
    # breaks both Jellyfin matching and TMDB search queries.
    t = re.sub(r"[.+&_]", " ", t)
    t = re.sub(r"\.", " ", t)
    t = re.sub(r"[\s_]+", " ", t)
    return t.strip()


# Words that legitimately follow a dash in series titles ("Dead Reckoning",
# "Chapter 2") must never be dropped as if they were release groups.
_KEEP_DASH_PARTS = {"chapter", "part", "volume", "special", "collection"}


def _drop_group_tags(text: str) -> str:
    """Drop trailing uploader/release-group segments like ' -Pah' / ' -Tinymkv Xyz'.

    Only removes a whitespace-dash-whitespace run when the surviving title still
    has at least 3 significant tokens, so real hyphenated series parts like
    "Mission Impossible - Dead Reckoning" are preserved.
    """
    m = re.search(r"\s+-\s*([A-Za-z][A-Za-z0-9]*)(?:\s+\w+)*\s*$", text)
    if not m:
        return text
    if m.group(1).lower() in _KEEP_DASH_PARTS:
        return text
    head = text[: m.start()].strip(" -.,")
    if len(re.findall(r"[a-z0-9]+", head.lower())) < 3:
        return text
    return head


def _remove_tokens(text: str, tokens: set) -> str:
    for tok in sorted(tokens, key=len, reverse=True):
        if len(tok) < 2:
            continue
        text = re.sub(
            r"(?<![a-z0-9])" + re.escape(tok) + r"(?![a-z0-9])", " ", text,
            flags=re.IGNORECASE,
        )
    return re.sub(r"\s+", " ", text).strip(" -.,")


# Junk words that are safe to consume in a trailing release run: OTT brands,
# language tokens, quality/codec/res tokens and generic "release word"
# suffixes. These are all also in `_ALL_STOP`, so `_remove_tokens` clears any
# that survive; the tail-stripper only exists to carry the adjacent channel
# DIGITS whose stop-words were already resolved.
_TAIL_RUN_WORDS = _ALL_LANG_TOKENS | _EXTRA_STOP | _STOP_TOKENS | {
    "blu", "ray", "dvdr", "web", "hdtv", "hdtvrip", "xvid", "divx", "wmv",
    "flac", "mp3", "dual", "subs", "proper", "repack", "extended", "uncut",
    "unrated",
}

# Short ISO 639-1 dub/language codes that appear as release tail tokens.
_ISO_LANG_CODES = {
    "en", "hi", "ml", "ta", "te", "kn", "bn", "pa", "mr", "gu", "ur", "ko",
    "ja", "zh", "es", "fr", "de", "ru", "ar", "pt", "id", "it",
}

# Audio channel markers ("6CH", "5.1", "2Ch", "DD5.1CH" -> "5 1 ch"). After
# `_strip_noise` these become "6ch", "5 1 ch"; a bare "NCH" token is never part
# of a real film title and must be removed from search queries ("Obsession
# 2025 1080p 6CH x265" -> "Obsession").
_CHANNEL_TOKEN_RE = re.compile(r"(?i)^\d{1,2}(?:\.\d)?ch$")

# TRUE audio/codec channel anchors. A trailing run is only dropped when it
# contains one of these, so quality tokens ("1080p", "x264") or a lone digit
# never eat a real number in a title ("K.G.F. Chapter 2", "Room 237").
_AUDIO_WORDS = {
    "dd", "dd5", "ddp", "ddp5", "ddplus", "dolby", "dts", "dtsx", "dtshd",
    "dtse", "dtsecore", "atmos", "truehd", "aac", "ac3", "ehq", "eac3",
}


def _strip_audio_lang_tail(text: str) -> str:
    """Drop a trailing audio/codec/language/platform run from a search query.

    Release names in any Telegram format carry release junk after the real
    title: "I Nobody Jiohotstar Dd+5.1 H (2026)", "Premam Dd5.1 1080p
    Malayalam", "Vikram.2022.1080p.DDP5.1.Tamil", "Interstellar (2014) 1080p
    DTS-MA Hindi". After `_strip_noise` those become space-separated tokens
    ("dd 5 1 h", "ddp5 1 tamil", "dts ma hin"). A trailing run of junk tokens
    (codec/dub digits included) is dropped only when it CONTAINS a true audio
    anchor word ("dd", "ddp5", "dts", "dts-ma", "atmos", ...), so real numbers
    in titles end up preserved:
      - "K.G.F.Chapter.2.1080p"      -> run "2 1080p", no audio anchor -> keep
      - "Manjummel.Boys.DDP5.1.Hindi" -> run "ddp5 1 hindi", anchor present -> drop
      - "I Nobody Jiohotstar Dd 5 1 H"-> run "dd 5 1 h", anchor present   -> drop
    """
    if not text:
        return text
    parts = text.split()
    i = len(parts) - 1
    run = []  # (token, is_audio_anchor)
    while i >= 0:
        tok = parts[i].lower().lstrip("-_.")
        npfx, dash, mark = tok.partition("-ma")
        is_channel = bool(_CHANNEL_TOKEN_RE.match(tok))
        is_anchor = (
            tok in _AUDIO_WORDS
            or (dash and npfx in _AUDIO_WORDS)
            or is_channel
        )
        is_ma = tok == "ma"
        is_digit = bool(re.fullmatch(r"\d[\d.]*", tok.strip(".-_")))
        is_short = (len(tok) == 1 and tok.isalpha()) or tok in _ISO_LANG_CODES
        is_word_junk = tok in _TAIL_RUN_WORDS or is_ma or is_short or is_channel
        if not (is_anchor or is_digit or is_word_junk):
            break
        run.append((tok, is_anchor))
        i -= 1

    if not run:
        return text
    # A trailing run is only dropped when it contains a true audio anchor.
    has_anchor = any(is_anchor for _, is_anchor in run)
    if not has_anchor:
        return text
    # Bare "ma" with an audio anchor on its left ("dts ma") counts into the run.
    head = parts[: i + 1]
    if len(re.findall(r"[a-z0-9]+", " ".join(head).lower())) < 1:
        return text
    return " ".join(head).strip()


def _detect_year(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    m = YEAR_RE.search(text)
    return int(m.group(0)) if m else None


def _clean_search_title(text: str) -> str:
    """A title ready to send to metadata providers (without release-group prefixes)."""
    # Remove known uploader/release-group prefixes only at the start. Do not
    # strip arbitrary tokens from the middle of a title or alter the preserved
    # source filename. In particular, "@WMR_SpiderMan.Homecoming..." becomes
    # "SpiderMan Homecoming..." before title scoring.
    text = re.sub(
        r"(?i)^\\s*(?:@?WMR|MM[._ -]*Linkz|PAHE(?:[._ -]*IN)?|YTS(?:[._ -]*MX)?|PSA|"
        r"TEAM[._ -]*GOAT[._ -]*BOT|PROSEARCH(?:[._ -]*BOT)?)[._ -]+",
        " ",
        str(text or ""),
    )
    text = _SIZE_RE.sub(" ", text)
    text = _strip_noise(_drop_dotted_group_tail(text))
    text = YEAR_RE.sub(" ", text)
    text = re.sub(r"\(\s*\)", " ", text)
    # Strip leading "Episode NN -" or "Ep NN -" prefix (e.g. "Episode 3 - Green Lantern")
    text = re.sub(r"(?i)^\s*(?:episode|ep)\.?\s*\d+\s*[-_.:]\s*", " ", text)
    # Remove audio/dub tails BEFORE generic token removal so codec words are
    # still present to anchor the trailing pattern.
    text = _strip_audio_lang_tail(text)
    text = _remove_tokens(text, _ALL_STOP)
    text = " ".join(t for t in text.split() if not _CHANNEL_TOKEN_RE.match(t))
    text = _drop_group_tags(text)
    # Strip descriptor noise words like "movie", "film" only when other words exist
    tokens = text.split()
    if len(tokens) > 1:
        non_desc = [w for w in tokens if w.lower() not in {"movie", "film", "cinema", "fullmovie", "full"}]
        if non_desc:
            tokens = non_desc
    # Strip trailing single-letter tags (e.g. 'A' from 'AVC A' or release suffixes)
    while len(tokens) > 1 and len(tokens[-1]) == 1 and tokens[-1].isalpha():
        tokens.pop()
    text = " ".join(tokens)
    return " ".join(text.lower().split())


def _significant_token_stream(text: str):
    """Yield non-stop words and sequel part numbers that are likely part of the real title."""
    seen = set()
    stop_tokens = _ALL_STOP
    cleaned = _SIZE_RE.sub(" ", text or "")
    cleaned = _strip_noise(_drop_dotted_group_tail(cleaned))
    for tok in re.findall(r"[a-z0-9]+", cleaned.lower()):
        if tok.isdigit():
            # Drop 4-digit years (e.g. 2013) or numbers > 2 digits (e.g. 780, 1080)
            if len(tok) == 4 or len(tok) > 2:
                continue
        elif tok in stop_tokens or len(tok) <= 1:
            continue
        if _CHANNEL_TOKEN_RE.match(tok):
            continue
        # Drop ambiguous alpha fragments ripped off a release token ("DTS-MA"->"ma",
        # "WEB-DL"->"dl") that add no matching value. Digits (sequels) are never checked here.
        if tok.isalpha() and len(tok) <= 2 and any(
            w != tok and (w.startswith(tok) or w.endswith(tok))
            for w in stop_tokens
        ):
            continue
        norm = _norm_tok(tok)
        if norm not in seen:
            seen.add(norm)
            yield norm


_TAGLINE_PATTERNS = re.compile(
    r"(?i)\b(?:"
    r"the\s+revenge|the\s+conclusion|the\s+beginning|the\s+final\s+chapter|"
    r"chapter\s*\d+|part\s*\d+|vol(?:ume)?\s*\d+|"
    r"returns|reloaded|rises|reborn|unrated|uncut|extended|"
    r"first\s+look|teaser|trailer|special\s+edition"
    r")\b"
)


def _search_candidates(raw: Optional[str]) -> List[str]:
    """Ordered candidate search queries for a raw Telegram file name.

    The fully-cleaned title is the most precise, but unforeseen release
    formats can leave OTT/codec junk in the middle of the string that a
    prefix-cleaning pass cannot see. We therefore also emit progressively
    simpler token queries that TMDB handles gracefully:
      - full significant-token stream (drops every stop/quality/dub token),
      - then leading-prefix sub-queries of that stream.
    Every candidate is still filtered through `best_tmdb_match`, so an
    over-broad query can never return a wrong hit.
    """
    if not raw:
        return []
    cands: List[str] = []
    cleaned = _clean_search_title(raw)
    if cleaned:
        cands.append(cleaned)
        tagless = _TAGLINE_PATTERNS.sub(" ", cleaned)
        tagless = re.sub(r"\s+", " ", tagless).strip()
        if tagless and tagless != cleaned and tagless not in cands:
            cands.append(tagless)
        if "and" in cleaned.split():
            with_amp = re.sub(r"\band\b", "&", cleaned)
            if with_amp not in cands:
                cands.append(with_amp)
    toks = list(_significant_token_stream(raw))
    full_toks = " ".join(toks[:12])
    if full_toks and full_toks != cleaned and full_toks not in cands:
        cands.append(full_toks)
    for n in (1, 2, 3, 4):
        prefix = " ".join(toks[:n])
        if prefix and prefix not in cands:
            cands.append(prefix)
    return cands


def _numeric_title_fallback(raw: str) -> str:
    """Recover a numeric-only title eaten by the year remover.

    MYSTERY_2018.2018.Malayalam.1080p -> "2018" (the film "2018"), while a real
    year ("2008", "2015") in the tag tail still reads as `year`. Rebuilds the
    first dotted numeric run when the stop-token pipeline emptied the title.
    """
    if not raw:
        return ""
    for token in re.split(r"[._\-\s]+", raw.strip()):
        if re.fullmatch(r"(?:19|20)\d{2}", token):
            return token
    return ""


def _extract_tv(filename: str):
    """Return (show_name, season, episode) if the name looks like a TV episode."""
    text = filename or ""
    for pattern in (_SEASON_EP, _SX_EX, _SX_E, _1X):
        m = pattern.search(text)
        if m:
            season = int(m.group(1))
            try:
                episode = int(m.group(2))
            except (IndexError, ValueError):
                episode = season  # Season-only pattern leaves episode unset
            before = text[: m.start()]
            return before, season, episode
    # Word/episode-only fallbacks require a show name prefix.
    for pattern in (_EPISODE_WORD, _EP_ONLY):
        m = pattern.search(text)
        if m:
            before = text[: m.start()].strip(" -.,_")
            if before and len(before.split()) >= 1:
                season = 1
                episode = int(m.group(1))
                return before, season, episode
    return None


def parse_media_meta(filename_or_caption: Optional[str]) -> Dict:
    """Robustly parse a Telegram file name / caption into media metadata.

    Returns a dict with:
      is_tv, title, show_name, season, episode, year,
      clean_title (folder-safe movie/show title w/ year removed),
      search_title (TMDB query), language
    """
    raw = _strip_noise(_drop_dotted_group_tail(filename_or_caption or ""))
    if not raw:
        return {
            "is_tv": False, "title": "Unknown_Media", "show_name": None,
            "season": None, "episode": None, "year": None,
            "clean_title": "Unknown_Media", "search_title": "", "language": DEFAULT_LANGUAGE,
        }

    year = _detect_year(raw)
    language = detect_language(raw)

    tv = _extract_tv(raw)
    if tv:
        show_raw, season, episode = tv
        clean_show = _clean_search_title(show_raw)
        if not clean_show:
            clean_show = "Unknown_Show"
        show_title = safe_folder(_clean_search_title(show_raw).title() or clean_show)
        title = f"{show_title} - S{int(season):02d}E{int(episode):02d}"
        return {
            "is_tv": True,
            "title": title,
            "show_name": show_title,
            "season": int(season),
            "episode": int(episode),
            "year": year,
            "clean_title": show_title,
            "search_title": clean_show,
            "search_candidates": _search_candidates(show_raw),
            "language": language,
        }

    movie_title = _clean_search_title(raw)
    if not movie_title:
        # Numeric-only/recovered titles: "2018.2018.Malayalam.1080p" is the
        # Malayalam film "2018", not a bare year; TMDB knows it by that name.
        movie_title = _numeric_title_fallback(filename_or_caption or raw)
    if not movie_title:
        movie_title = "Unknown_Media"
    display = safe_folder(movie_title.title() or movie_title)
    return {
        "is_tv": False,
        "title": display,
        "show_name": None,
        "season": None,
        "episode": None,
        "year": year,
        "clean_title": display,
        "search_title": movie_title,
        "search_candidates": _search_candidates(raw),
        "language": language,
    }


_INDIAN_LANG_CODES = {"ml", "ta", "te", "hi", "kn", "bn", "pa", "mr", "gu", "ur"}


def best_tmdb_match(results: List[Dict], query: str, media_type: str,
                    year: Optional[int] = None, threshold: float = 0.70,
                    language: Optional[str] = None):
    """Return the best TMDB search result for (query, year, type), or None.

    Scoring: token overlap against the hit's title merged with its
    original_title. Uses Dice coefficient to balance query precision and hit length.
    Year proximity provides strong signal: exact year +0.35, ±1 year +0.15,
    >2 years off heavily penalized.
    Regional language affinity: if release language is Indian regional (Tamil, Malayalam, etc.)
    and hit language is a non-Indian foreign language (Portuguese, German, etc.),
    a strong mismatch penalty is applied to prevent false-positive hijacking.
    Ambiguity guard: if top candidates have scores within 0.05 without a distinguishing
    attribute, returns None to avoid guessing.
    """
    q_toks = _sig_tokens(query)
    if not q_toks:
        return None
    lang_raw = (language or "").strip()
    lang = tmdb_language_code(lang_raw) or lang_raw.lower()
    is_indian = lang in _INDIAN_LANG_CODES
    scored: List[Tuple[float, Dict]] = []
    for r in results or []:
        mt = r.get("media_type")
        if mt and mt != media_type:
            continue
        name = r.get("title") or r.get("name") or ""
        r_toks = _sig_tokens(name)
        orig_name = r.get("original_title") or r.get("original_name") or ""
        orig_toks = _sig_tokens(orig_name)
        combined_r = r_toks | orig_toks
        if not combined_r:
            continue
        overlap = len(q_toks & combined_r)
        if not overlap:
            continue

        extra_in_hit = combined_r - q_toks
        franchise_subwords = {"part", "chapter", "vol", "volume", "1", "2", "3", "4", "5", "the"}
        extra_unrelated = extra_in_hit - franchise_subwords

        # If either title or original_title is an exact match for the query,
        # the other field is simply the native/alternate title and does not introduce unrelated words.
        if r_toks == q_toks or orig_toks == q_toks:
            extra_unrelated = set()

        # Single-token query strictness:
        # A 1-word query (e.g. "Khalifa", "Money", "Paradise") must NEVER match a multi-word candidate
        # with foreign/unrelated words (e.g. "Queer Khalifa", "The Siege of Paradise", "Derry Girls")!
        if len(q_toks) == 1 and extra_unrelated:
            # Only allow if the hit's main or original title is strictly a 1-token match
            if r_toks != q_toks and orig_toks != q_toks:
                continue

        # Check containment: either all hit tokens are in query (e.g. "Aadu 3" in "Aadu 3 Part 1"),
        # or all query tokens are in hit title without introducing unrelated words
        hit_contained_in_query = bool(combined_r <= q_toks) or bool(r_toks == q_toks or orig_toks == q_toks)
        query_contained_in_hit = bool(q_toks <= combined_r and not extra_unrelated)

        # Sequel digit alignment:
        q_digits = {t for t in q_toks if t.isdigit()}
        r_digits = {t for t in combined_r if t.isdigit()}

        # If query has sequel digits (e.g. 2, 3), hit MUST also have digits and share at least one!
        # A sequel query (e.g. "Aadu 3" or "Iron Man 3") must NEVER match an unnumbered first film (e.g. "Aadu" or "Iron Man")!
        if q_digits and not (q_digits & r_digits):
            continue

        # If query has NO sequel digits (original movie), hit must NOT be a numbered sequel (e.g. Movie 2)
        if r_digits and not q_digits:
            continue

        # Multi-token overlap requirements:
        if not hit_contained_in_query and not (q_toks == combined_r):
            # A 2-word title (e.g. "Iron Man") must match both words; matching only 1 (e.g. "Generation Iron") is a false positive
            if len(q_toks) == 2 and overlap < 2:
                continue
            # A 3+ word title (e.g. "Iron Man 3") must match at least 2 tokens
            if len(q_toks) >= 3 and overlap < 2:
                continue

        sim_main = (2.0 * len(q_toks & r_toks)) / (len(q_toks) + len(r_toks)) if r_toks else 0.0
        sim_orig = (2.0 * len(q_toks & orig_toks)) / (len(q_toks) + len(orig_toks)) if orig_toks else 0.0
        sim_comb = (2.0 * overlap) / (len(q_toks) + len(combined_r))
        score = max(sim_main, sim_orig, sim_comb)

        if hit_contained_in_query or query_contained_in_hit:
            score += 0.35

        if q_toks <= combined_r:
            if len(q_toks) == len(combined_r):
                score += 0.3
            else:
                score += 0.15

        if extra_unrelated:
            score -= 0.35 * len(extra_unrelated)

        diff = None
        date = (r.get("release_date") or r.get("first_air_date") or "")[:4]
        if year and date and date.isdigit():
            hit_year = int(date)
            diff = abs(hit_year - year)
            if diff == 0:
                score += 0.35
            elif diff == 1:
                score += 0.15
            elif diff == 2:
                # If titles don't match 100%, 2-year difference is rejected
                if extra_unrelated or overlap < len(q_toks):
                    continue
                score -= 0.35
            elif diff <= 5:
                # Allow exact title match or franchise (e.g. numeric titles like 2018 or delayed festival releases)
                if extra_unrelated or overlap < len(q_toks):
                    continue
                score -= 0.45
            elif diff <= 8:
                if extra_unrelated or overlap < len(q_toks):
                    continue
                score -= 0.85
            else:
                # 9+ year gap is an entirely different era / film!
                # Only exempt if the query itself is a 4-digit number (e.g. film "2018" released in 2023)
                if not (query.strip().isdigit() and len(query.strip()) == 4):
                    continue
                score -= 0.60

        hit_lang = (r.get("original_language") or "").lower()
        if lang:
            if hit_lang == lang:
                score += 0.35
            elif is_indian and hit_lang in _INDIAN_LANG_CODES:
                score += 0.15
            elif is_indian and hit_lang == "en":
                # An Indian regional release (Malayalam, Tamil, etc.) matching an English movie
                # must not happen for short/generic queries, when extra words exist, or when year differs.
                if diff is not None and diff > 0:
                    continue
                if extra_unrelated or len(q_toks) <= 2:
                    continue
                score -= 0.5
            elif is_indian and hit_lang not in _INDIAN_LANG_CODES:
                # An Indian regional release must NEVER match an unrelated foreign language
                continue
            elif not is_indian and lang == "en" and hit_lang not in {"en", ""}:
                score -= 0.35

        # Boost real distributed films with popularity/votes/artwork over zero-vote placeholders
        try:
            pop = float(r.get("popularity") or 0.0)
            votes = int(r.get("vote_count") or 0)
            if pop >= 5.0 or votes >= 10:
                score += 0.20
            if r.get("poster_path"):
                score += 0.10
            if r.get("backdrop_path") or r.get("overview"):
                score += 0.05
            if not r.get("poster_path") and votes == 0:
                score -= 0.15
        except (ValueError, TypeError):
            pass

        scored.append((score, r))

    if not scored:
        return None

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best = scored[0]
    if best_score < threshold:
        return None

    if len(scored) > 1:
        second_score, second = scored[1]
        # Ambiguity detection: if top 2 candidates are virtually tied (score difference < 0.05)
        if (best_score - second_score) < 0.05:
            # Check if duplicate result returned for the same ID
            if best.get("id") and best.get("id") == second.get("id"):
                return best
            best_date = (best.get("release_date") or best.get("first_air_date") or "")[:4]
            sec_date = (second.get("release_date") or second.get("first_air_date") or "")[:4]
            best_year_exact = bool(year and best_date.isdigit() and int(best_date) == year)
            sec_year_exact = bool(year and sec_date.isdigit() and int(sec_date) == year)
            if best_year_exact and not sec_year_exact:
                return best
            # Indistinguishable candidates: reject as ambiguous rather than guessing
            return None

    return best


def tmdb_image_url(path: Optional[str], size: str = "w500") -> Optional[str]:
    if not path:
        return None
    return f"{TPL_BASE}/{size}{path}"


def build_movie_nfo(data: Dict) -> str:
    g = lambda k: str(data.get(k) or "")
    genres = [_gen(x) for x in (data.get("genres") or []) if _gen(x)]
    country = ""
    for c in data.get("production_countries") or []:
        if c.get("iso_3166_1"):
            country = c["iso_3166_1"]
            break
    genre_lines = "".join("  <genre>%s</genre>\n" % xml_escape(x) for x in genres)
    country_line = "  <country>%s</country>\n" % xml_escape(country) if country else ""
    imdb_id = xml_escape(str(data.get("imdb_id") or ""))
    imdb_unique = f'  <uniqueid type="imdb" default="true">{imdb_id}</uniqueid>\n' if imdb_id else ""
    return """<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<movie>
  <title>%(title)s</title>
  <originaltitle>%(original_title)s</originaltitle>
  <year>%(year)s</year>
  <premiered>%(premiered)s</premiered>
  <releasedate>%(releasedate)s</releasedate>
  <rating>%(rating)s</rating>
  <votes>%(votes)s</votes>
  <runtime>%(runtime)s</runtime>
  <plot>%(plot)s</plot>
  <tagline>%(tagline)s</tagline>
  <lockdata>true</lockdata>
  <imdbid>%(imdbid)s</imdbid>
  <tmdbid>%(tmdbid)s</tmdbid>
  <uniqueid type="tmdb">%(tmdbid)s</uniqueid>
%(imdb_unique)s  %(genres)s%(country)s  <art>
    <poster>poster.jpg</poster>
    <backdrop>backdrop.jpg</backdrop>
  </art>
</movie>
""" % {
        "title": xml_escape(g("title")),
        "original_title": xml_escape(g("original_title")),
        "year": xml_escape(g("release_date"))[:4],
        "premiered": xml_escape(g("release_date")),
        "releasedate": xml_escape(g("release_date")),
        "rating": xml_escape(g("vote_average")),
        "votes": xml_escape(g("vote_count")),
        "runtime": xml_escape(g("runtime")),
        "plot": xml_escape(g("overview")),
        "tagline": xml_escape(g("tagline")),
        "imdbid": imdb_id,
        "tmdbid": xml_escape(str(data.get("id") or "")),
        "imdb_unique": imdb_unique,
        "genres": genre_lines,
        "country": country_line,
    }


def build_tvshow_nfo(data: Dict) -> str:
    g = lambda k: str(data.get(k) or "")
    genres = [_gen(x) for x in (data.get("genres") or []) if _gen(x)]
    genre_lines = "".join("  <genre>%s</genre>\n" % xml_escape(x) for x in genres)
    imdb_id = xml_escape(str(data.get("imdb_id") or data.get("imdbid") or ""))
    tmdb_id = xml_escape(str(data.get("id") or data.get("tmdb_id") or data.get("tmdbid") or ""))
    imdb_unique = f'  <uniqueid type="imdb" default="true">{imdb_id}</uniqueid>\n' if imdb_id else ""
    tmdb_unique = f'  <uniqueid type="tmdb">{tmdb_id}</uniqueid>\n' if tmdb_id else ""
    title_val = g("title") or g("name") or g("original_name") or g("original_title") or "Unknown"
    orig_title_val = g("original_name") or g("original_title") or title_val
    premiered_val = g("first_air_date") or g("premiered") or g("release_date") or ""
    year_val = str(data.get("year") or "") or premiered_val[:4]
    plot_val = g("overview") or g("plot") or ""
    return """<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<tvshow>
  <title>%(title)s</title>
  <originaltitle>%(original_title)s</originaltitle>
  <year>%(year)s</year>
  <premiered>%(premiered)s</premiered>
  <rating>%(rating)s</rating>
  <votes>%(votes)s</votes>
  <runtime>%(runtime)s</runtime>
  <plot>%(plot)s</plot>
  <lockdata>true</lockdata>
  <imdbid>%(imdbid)s</imdbid>
  <tmdbid>%(tmdbid)s</tmdbid>
%(tmdb_unique)s%(imdb_unique)s  %(genres)s  <art>
    <poster>poster.jpg</poster>
    <backdrop>backdrop.jpg</backdrop>
  </art>
</tvshow>
""" % {
        "title": xml_escape(title_val),
        "original_title": xml_escape(orig_title_val),
        "year": xml_escape(year_val),
        "premiered": xml_escape(premiered_val),
        "rating": xml_escape(g("vote_average") or g("rating")),
        "votes": xml_escape(g("vote_count") or g("votes")),
        "runtime": xml_escape(_tv_runtime(data)),
        "plot": xml_escape(plot_val),
        "imdbid": imdb_id,
        "tmdbid": tmdb_id,
        "tmdb_unique": tmdb_unique,
        "imdb_unique": imdb_unique,
        "genres": genre_lines,
    }


def build_episode_nfo(data: Dict) -> str:
    g = lambda k: str(data.get(k) or "")
    return """<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<episodedetails>
  <season>%(season)s</season>
  <episode>%(episode)s</episode>
  <title>%(title)s</title>
  <plot>%(plot)s</plot>
  <aired>%(aired)s</aired>
  <rating>%(rating)s</rating>
  <runtime>%(runtime)s</runtime>
  <lockdata>true</lockdata>
  <uniqueid type="tmdb">%(tmdbid)s</uniqueid>
</episodedetails>
""" % {
        "season": xml_escape(g("season_number")),
        "episode": xml_escape(g("episode_number")),
        "title": xml_escape(g("name")),
        "plot": xml_escape(g("overview")),
        "aired": xml_escape(g("air_date")),
        "rating": xml_escape(g("vote_average")),
        "runtime": xml_escape(g("runtime")),
        "tmdbid": xml_escape(str(data.get("id") or "")),
    }


def _gen(obj) -> str:
    return str(obj.get("name") or "") if isinstance(obj, dict) else ""


def _tv_runtime(data: Dict) -> str:
    rt = data.get("episode_run_time") or []
    return str(rt[0]) if rt else ""