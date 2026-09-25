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
from typing import Dict, List, Optional

from xml.sax.saxutils import escape as xml_escape

from .organize import (
    _ALL_LANG_TOKENS,
    _STOP_TOKENS,
    DEFAULT_LANGUAGE,
    detect_language,
    safe_folder,
)

TPL_BASE = "https://image.tmdb.org/t/p"

YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")

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
    "10bit", "8bit", "50fps", "60fps", "multisub", "truehd", "ddplus", "ddp5",
    "avc", "h264", "h265", "x264", "x265", "hevc", "itunes", "max", "disney",
    "disneyplus", "hulu", "paramount", "cmc", "hbo", "hbomax", "amazon",
    "googleplay", "sd", "hq", "ts", "cam", "hdcam", "blu-ray", "bluray",
    "hdrip", "dvdrip", "dvd", "webm", "mkv", "mp4",
}

_ALL_STOP = _STOP_TOKENS | _EXTRA_STOP | _ALL_LANG_TOKENS


def _sig_tokens(text: str) -> set:
    toks = {t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _ALL_STOP}
    return {t for t in toks if not (t.isdigit() and len(t) == 4)}


def _clean_check(text: str) -> str:
    """Lowercased token check description used for year/tag stripping."""
    return text


def _strip_noise(text: str) -> str:
    """Strip group tags ([...], @Handle) and collapse whitespace."""
    t = re.sub(r"\[[^\]]*\]", " ", text)
    t = re.sub(r"@[\w.\-]+", " ", t)
    t = re.sub(r"[\s_]+", " ", t)
    return t.strip()


def _remove_tokens(text: str, tokens: set) -> str:
    for tok in sorted(tokens, key=len, reverse=True):
        if len(tok) < 3:
            continue
        text = re.sub(
            r"(?<![a-z0-9])" + re.escape(tok) + r"(?![a-z0-9])", " ", text,
            flags=re.IGNORECASE,
        )
    return re.sub(r"\s+", " ", text).strip(" -.,")


def _detect_year(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    m = YEAR_RE.search(text)
    return int(m.group(0)) if m else None


def _clean_search_title(text: str) -> str:
    """A title ready to send to the TMDB search API (no year/release tags)."""
    text = _strip_noise(text or "")
    text = YEAR_RE.sub(" ", text)
    text = _remove_tokens(text, _ALL_STOP)
    return " ".join(text.lower().split())


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
    raw = _strip_noise(filename_or_caption or "")
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
            "language": language,
        }

    movie_title = _clean_search_title(raw)
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
        "language": language,
    }


def best_tmdb_match(results: List[Dict], query: str, media_type: str,
                    year: Optional[int] = None, threshold: float = 0.5):
    """Return the best TMDB search result for (query, year, type), or None.

    Scoring: token containment of the query against the hit title, with a boost
    when the hit's release/first-air year equals the parsed file year. Results
    of the wrong media_type are excluded so movies never steal a series match.
    """
    q_toks = _sig_tokens(query)
    if not q_toks:
        return None
    best, best_score = None, -1.0
    for r in results or []:
        mt = r.get("media_type")
        if mt and mt != media_type:
            continue
        name = r.get("title") or r.get("name") or ""
        r_toks = _sig_tokens(name)
        if not r_toks:
            continue
        overlap = len(q_toks & r_toks)
        if not overlap:
            continue
        score = overlap / max(1, min(len(q_toks), len(r_toks)))
        date = (r.get("release_date") or r.get("first_air_date") or "")[:4]
        if year and date and int(date) == year:
            score += 0.3
        if q_toks <= r_toks:
            score += 0.3
        if score > best_score:
            best, best_score = r, score
    return best if best_score >= threshold else None


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
    return f"""<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<movie>
  <title>{xml_escape(g('title'))}</title>
  <originaltitle>{xml_escape(g('original_title'))}</originaltitle>
  <year>{xml_escape(g('release_date'))[:4]}</year>
  <premiered>{xml_escape(g('release_date'))}</premiered>
  <releasedate>{xml_escape(g('release_date'))}</releasedate>
  <rating>{xml_escape(g('vote_average'))}</rating>
  <votes>{xml_escape(g('vote_count'))}</votes>
  <runtime>{xml_escape(g('runtime'))}</runtime>
  <plot>{xml_escape(g('overview'))}</plot>
  <tagline>{xml_escape(g('tagline'))}</tagline>
  <imdbid>{xml_escape(str(data.get('imdb_id') or ''))}</imdbid>
  <tmdbid>{xml_escape(str(data.get('id') or ''))}</tmdbid>
  <uniqueid type="tmdb">{xml_escape(str(data.get('id') or ''))}</uniqueid>
  {''.join(f'  <genre>{xml_escape(x)}</genre>\n' for x in genres)}
  {f'  <country>{xml_escape(country)}</country>\n' if country else ''}
  <art>
    <poster>poster.jpg</poster>
    <backdrop>backdrop.jpg</backdrop>
  </art>
</movie>
"""


def build_tvshow_nfo(data: Dict) -> str:
    g = lambda k: str(data.get(k) or "")
    genres = [_gen(x) for x in (data.get("genres") or []) if _gen(x)]
    return f"""<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<tvshow>
  <title>{xml_escape(g('name'))}</title>
  <originaltitle>{xml_escape(g('original_name'))}</originaltitle>
  <year>{xml_escape(g('first_air_date'))[:4]}</year>
  <premiered>{xml_escape(g('first_air_date'))}</premiered>
  <rating>{xml_escape(g('vote_average'))}</rating>
  <votes>{xml_escape(g('vote_count'))}</votes>
  <runtime>{xml_escape(_tv_runtime(data))}</runtime>
  <plot>{xml_escape(g('overview'))}</plot>
  <tmdbid>{xml_escape(str(data.get('id') or ''))}</tmdbid>
  <uniqueid type="tmdb">{xml_escape(str(data.get('id') or ''))}</uniqueid>
  {''.join(f'  <genre>{xml_escape(x)}</genre>\n' for x in genres)}
  <art>
    <poster>poster.jpg</poster>
    <backdrop>backdrop.jpg</backdrop>
  </art>
</tvshow>
"""


def build_episode_nfo(data: Dict) -> str:
    g = lambda k: str(data.get(k) or "")
    return f"""<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<episodedetails>
  <season>{xml_escape(g('season_number'))}</season>
  <episode>{xml_escape(g('episode_number'))}</episode>
  <title>{xml_escape(g('name'))}</title>
  <plot>{xml_escape(g('overview'))}</plot>
  <aired>{xml_escape(g('air_date'))}</aired>
  <rating>{xml_escape(g('vote_average'))}</rating>
  <runtime>{xml_escape(g('runtime'))}</runtime>
  <uniqueid type="tmdb">{xml_escape(str(data.get('id') or ''))}</uniqueid>
</episodedetails>
"""


def _gen(obj) -> str:
    return str(obj.get("name") or "") if isinstance(obj, dict) else ""


def _tv_runtime(data: Dict) -> str:
    rt = data.get("episode_run_time") or []
    return str(rt[0]) if rt else ""