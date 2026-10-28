"""Regression tests for tg_streamer._prune_orphan_strms (layout-migration dedup).

tg_streamer imports pyrogram which isn't installed locally, so the helper is
extracted from the source AST and executed against temp directories.
"""

import ast
import asyncio
import os
import re
import shutil
import uuid
from typing import Optional, Dict
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "tg_streamer.py"

STREAMER_URL = "http://127.0.0.1:8080/stream/-100:9/video.mp4"
OTHER_URL = "http://example.com/file.strm"


def _helper():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    node = next(
        n for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_prune_orphan_strms"
    )
    ns = {"logger": type("_Sink", (), {"info": lambda *a, **k: None, "warning": lambda *a, **k: None})(), "os": os, "shutil": shutil}
    exec(compile(ast.Module([node], []), "<ast>", "exec"), ns)
    return ns["_prune_orphan_strms"]


def _write(root, rel, content):
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def _run(prune, movies, shows, keep):
    return asyncio.run(prune(movies, shows, keep))


def test_prune_removes_orphan_keeps_expected(tmp_path):
    prune = _helper()
    movies = tmp_path / "Movies"
    shows = tmp_path / "TV Shows"
    # New per-title layout (expected).
    keep_dir = movies / "English" / "Spiderman Homecoming (2017)"
    keep_dir.mkdir(parents=True)
    keep_strm = keep_dir / "Spiderman Homecoming (2017).strm"
    keep_strm.write_text(STREAMER_URL)
    keep_path = str(keep_strm.resolve())
    # Old flat layout orphan -> must be pruned.
    old_strm = _write(movies, "English/SpiderMan.Homecoming.2017.720p.BluRay.x264.strm", STREAMER_URL)
    # Unrelated strm (not ours) -> must survive.
    external = _write(movies, "English/someone-elses.strm", OTHER_URL)
    # TV flat-layout orphan in wrong (no-year) folder.
    tv_orphan = _write(shows, "English/Breaking Bad/Season 01/Breaking Bad - S01E01.strm", STREAMER_URL)

    pruned = _run(prune, str(movies), str(shows), {keep_path})
    assert pruned == 2
    assert keep_strm.exists()
    assert external.exists()
    assert not old_strm.exists()
    assert not tv_orphan.exists()


def test_prune_keeps_everything_when_all_expected(tmp_path):
    prune = _helper()
    movies = tmp_path / "Movies"
    keep_dir = movies / "English" / "Dune (2021)"
    keep_dir.mkdir(parents=True)
    s = keep_dir / "Dune (2021).strm"
    s.write_text(STREAMER_URL)
    pruned = _run(prune, str(movies), str(tmp_path / "TV Shows"), {str(s.resolve())})
    assert pruned == 0
    assert s.exists()


def test_purge_media_item_by_title_and_msg_id(tmp_path):
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    node = next(
        n for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "purge_media_item"
    )
    cache = {}
    catalog_deleted = []

    class MockCatalog:
        def mark_deleted(self, source_chat_id, source_message_id):
            catalog_deleted.append((source_chat_id, source_message_id))
            return True

    movie_dir = tmp_path / "Movie (2026)"
    movie_dir.mkdir()
    strm_file = movie_dir / "Movie (2026).strm"
    strm_file.write_text("http://127.0.0.1:8080/stream/1:100/video.mp4")

    cache["-100:100"] = {
        "chat_id": -100,
        "msg_id": 100,
        "title": "Movie (2026)",
        "clean_title": "Movie",
        "strm_path": str(strm_file),
        "target_dir": str(movie_dir),
    }

    async def mock_save():
        pass

    async def mock_scan():
        pass

    ns = {
        "logger": type("_Sink", (), {"info": lambda *a, **k: None, "warning": lambda *a, **k: None})(),
        "os": os,
        "re": re,
        "shutil": shutil,
        "Optional": Optional,
        "_split_cache_key": lambda k: (int(k.split(":")[0]), int(k.split(":")[1])),
        "FILE_ID_CACHE": cache,
        "MEDIA_CATALOG": MockCatalog(),
        "save_cache_async": mock_save,
        "trigger_jellyfin_scan": mock_scan,
    }
    exec(compile(ast.Module([node], []), "<ast>", "exec"), ns)
    purge = ns["purge_media_item"]

    purged = asyncio.run(purge(title_query="Movie"))
    assert purged == ["Movie (2026)"]
    assert not strm_file.exists()
    assert not movie_dir.exists()
    assert len(cache) == 0
    assert catalog_deleted == [(-100, 100)]


def test_deduplicate_jellyfin_items_preserves_blacklisted_media(tmp_path):
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    node = next(
        n for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "deduplicate_jellyfin_items"
    )

    deleted_item_ids = []
    recorded_reviews = []

    class MockResponse:
        def __init__(self, data=None, status=200):
            self.data = data or {}
            self.status = status

        async def json(self):
            return self.data

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class MockSession:
        def __init__(self, items):
            self.items = items

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def get(self, url, **kwargs):
            return MockResponse({"Items": self.items}, status=200)

        def delete(self, url, **kwargs):
            item_id = url.rsplit("/", 1)[-1]
            deleted_item_ids.append(item_id)
            return MockResponse(status=204)

    class MockAiohttp:
        def __init__(self, items):
            self.items = items

        def ClientSession(self, *args, **kwargs):
            return MockSession(self.items)

    movies_dir = tmp_path / "Movies"
    shows_dir = tmp_path / "TV Shows"
    movies_dir.mkdir(parents=True)
    shows_dir.mkdir(parents=True)

    # Physical strm file for blacklisted movie
    blacklisted_folder = movies_dir / "Ms.Rose & Mr.Cane (2026)"
    blacklisted_folder.mkdir()
    blacklisted_strm = blacklisted_folder / "Ms.Rose & Mr.Cane (2026).strm"
    blacklisted_strm.write_text("http://127.0.0.1:8080/stream/1:200/video.mp4")

    # Physical dir for blacklisted series
    blacklisted_series = shows_dir / "WWE Money in the Bank"
    blacklisted_series.mkdir()

    # Physical strm for legitimate duplicates
    legit_folder = movies_dir / "Legit Movie (2025)"
    legit_folder.mkdir()
    legit_strm = legit_folder / "Legit Movie (2025).strm"
    legit_strm.write_text("http://127.0.0.1:8080/stream/1:300/video.mp4")

    items = [
        {
            "Id": "blacklisted-movie-id",
            "Name": "Ms.Rose & Mr.Cane",
            "Type": "Movie",
            "Path": str(blacklisted_strm),
            "ProviderIds": {},
        },
        {
            "Id": "blacklisted-series-id",
            "Name": "WWE Money in the Bank",
            "Type": "Series",
            "Path": str(blacklisted_series),
            "ProviderIds": {},
        },
        {
            "Id": "legit-dup-1",
            "Name": "Legit Movie",
            "Type": "Movie",
            "Path": str(legit_strm),
            "ProviderIds": {"Tmdb": "9999"},
        },
        {
            "Id": "legit-dup-2",
            "Name": "Legit Movie",
            "Type": "Movie",
            "Path": str(legit_strm),
            "ProviderIds": {"Tmdb": "9999"},
        },
        {
            "Id": "ghost-orphan-id",
            "Name": "Deleted Media",
            "Type": "Movie",
            "Path": str(movies_dir / "nonexistent.strm"),
            "ProviderIds": {},
        },
    ]

    # Extract is_blacklisted via AST to avoid importing pyrogram
    patterns_node = next(
        n for n in tree.body
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_BLACKLIST_PATTERNS" for t in n.targets)
    )
    is_bl_node = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "is_blacklisted"
    )
    bl_ns = {"re": re, "Optional": Optional}
    exec(compile(ast.Module([patterns_node, is_bl_node], []), "<ast>", "exec"), bl_ns)
    is_blacklisted = bl_ns["is_blacklisted"]

    ns = {
        "logger": type("_Sink", (), {"info": lambda *a, **k: None, "warning": lambda *a, **k: None})(),
        "os": os,
        "re": re,
        "shutil": shutil,
        "aiohttp": MockAiohttp(items),
        "_jellyfin_auth_header": lambda: {"X-Emby-Token": "test-token"},
        "_read_jellyfin_api_key": lambda: "test-token",
        "MOVIES_DIR": str(movies_dir),
        "SHOWS_DIR": str(shows_dir),
        "FILE_ID_CACHE": {},
        "is_blacklisted": is_blacklisted,
        "_record_review_job": lambda data, confidence=0.0, reason="": recorded_reviews.append((data, confidence, reason)),
        "parse_media_meta": lambda name: {"title": name, "clean_title": name},
        "_canonical_media_key": lambda e: "tmdb:9999",
        "DEFAULT_LANGUAGE": "English",
        "detect_language": lambda t: "English",
        "extract_version_tag": lambda p: "Alt",
        "_save_cache_sync": lambda: None,
    }
    exec(compile(ast.Module([node], []), "<ast>", "exec"), ns)
    dedup = ns["deduplicate_jellyfin_items"]

    res = asyncio.run(dedup())

    # Blacklisted items MUST NEVER be deleted from Jellyfin
    assert "blacklisted-movie-id" not in deleted_item_ids
    assert "blacklisted-series-id" not in deleted_item_ids

    # Both blacklisted media on disk MUST be preserved
    assert blacklisted_strm.exists()
    assert blacklisted_series.exists()

    # Blacklisted items MUST be routed to the review queue
    review_reasons = [r[2] for r in recorded_reviews]
    assert any("blacklisted_jellyfin_item:Ms.Rose & Mr.Cane" in r for r in review_reasons)
    assert any("blacklisted_jellyfin_item:WWE Money in the Bank" in r for r in review_reasons)

    # Genuine orphan item (nonexistent file) must be deleted
    assert "ghost-orphan-id" in deleted_item_ids

    # Exactly one duplicate of the legitimate movie must be deleted
    assert ("legit-dup-1" in deleted_item_ids) ^ ("legit-dup-2" in deleted_item_ids)


def test_reindex_all_with_imdb_preserves_untracked_strm_and_directories(tmp_path):
    """Execute the real reindex_all_with_imdb function and verify that both untracked .strm files
    and directories with unrecognized formats are preserved and sent to review.
    """
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    node = next(
        n for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "reindex_all_with_imdb"
    )

    movies_dir = tmp_path / "Movies"
    shows_dir = tmp_path / "TV Shows"
    movies_dir.mkdir(parents=True)
    shows_dir.mkdir(parents=True)

    # 1. Tracked media folder & strm
    tracked_folder = movies_dir / "English" / "Tracked Title (2025)"
    tracked_folder.mkdir(parents=True)
    tracked_strm = tracked_folder / "Tracked Title (2025).strm"
    tracked_strm.write_text("http://127.0.0.1:8080/stream/1:100/video.mp4")

    # 2. Untracked directory containing an unrecognized binary file (.bin)
    untracked_unknown_dir = movies_dir / "English" / "Unknown Format Title"
    untracked_unknown_dir.mkdir(parents=True)
    bin_file = untracked_unknown_dir / "payload.bin"
    bin_file.write_bytes(b"valuable media payload")

    # 3. Untracked .strm file in another directory
    untracked_strm_dir = movies_dir / "English" / "Untracked Stream Title"
    untracked_strm_dir.mkdir(parents=True)
    untracked_strm = untracked_strm_dir / "untracked.strm"
    untracked_strm.write_text("http://127.0.0.1:8080/stream/1:999/video.mp4")

    # 4. Untracked directory with sidecar files (.nfo, .txt)
    sidecar_dir = movies_dir / "English" / "Sidecar Title"
    sidecar_dir.mkdir(parents=True)
    (sidecar_dir / "movie.nfo").write_text("<movie></movie>")
    (sidecar_dir / "notes.txt").write_text("user notes")

    # 5. Genuinely empty directory (no files, no subdirectories)
    truly_empty = movies_dir / "English" / "Truly Empty Folder"
    truly_empty.mkdir(parents=True)

    recorded_reviews = []

    class MockJob:
        payload = {
            "strm_path": str(tracked_strm),
            "target_dir": str(tracked_folder),
        }

    class MockRecord:
        source_chat_id = -100
        source_message_id = 100

    class MockCatalog:
        def list_records(self, include_deleted=False):
            return [MockRecord()]

        def find_job(self, chat_id, msg_id):
            return MockJob()

    class MockSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    class MockAiohttp:
        class TCPConnector:
            def __init__(self, *args, **kwargs):
                pass
        def ClientSession(self, *args, **kwargs):
            return MockSession()

    async def mock_async_noop(*args, **kwargs):
        pass

    import socket
    ns = {
        "logger": type("_Sink", (), {"info": lambda *a, **k: None, "warning": lambda *a, **k: None})(),
        "os": os,
        "socket": socket,
        "aiohttp": MockAiohttp(),
        "asyncio": asyncio,
        "MOVIES_DIR": str(movies_dir),
        "SHOWS_DIR": str(shows_dir),
        "FILE_ID_CACHE": {},
        "MEDIA_CATALOG": MockCatalog(),
        "save_cache_async": mock_async_noop,
        "deduplicate_jellyfin_items": mock_async_noop,
        "refresh_jellyfin_item_by_path": mock_async_noop,
        "request_jellyfin_scan": mock_async_noop,
        "_record_review_job": lambda data, confidence=0.0, reason="": recorded_reviews.append((data, confidence, reason)),
    }
    exec(compile(ast.Module([node], []), "<ast>", "exec"), ns)
    reindex_func = ns["reindex_all_with_imdb"]

    # Execute the actual reindex_all_with_imdb function
    res = asyncio.run(reindex_func(force_all=False))

    # Untracked directory containing unrecognized file MUST survive!
    assert untracked_unknown_dir.exists()
    assert bin_file.exists()

    # Untracked .strm file and its directory MUST survive!
    assert untracked_strm_dir.exists()
    assert untracked_strm.exists()

    # Tracked media must remain untouched
    assert tracked_folder.exists()
    assert tracked_strm.exists()

    # Sidecar directory MUST survive and be routed to review
    assert sidecar_dir.exists()
    assert (sidecar_dir / "movie.nfo").exists()
    assert (sidecar_dir / "notes.txt").exists()

    # Truly empty directory MUST be cleaned via os.rmdir
    assert not truly_empty.exists()

    # Review queue must have recorded both untracked directories and untracked strm
    reasons = [r[2] for r in recorded_reviews]
    assert any("untracked_directory:Unknown Format Title" in r for r in reasons)
    assert any("untracked_directory:Sidecar Title" in r for r in reasons)
    assert any("untracked_strm_file:untracked.strm" in r for r in reasons)


def test_reconciliation_end_to_end_library_behavior(tmp_path):
    """End-to-end integration test verifying that:
    1. Tracked media survives.
    2. Untracked movie .strm and its directory survive and are routed to review.
    3. Duplicate Jellyfin items are deduplicated safely.
    4. Missing .strm target (genuine orphan) is cleaned from Jellyfin API.
    5. User-created sidecars (notes.txt, custom.xml, cover_backup.png) survive intact.
    6. Unknown / uncertain media entries are preserved and sent to review.
    """
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    dedup_node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "deduplicate_jellyfin_items")
    reindex_node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "reindex_all_with_imdb")

    movies_dir = tmp_path / "Movies"
    shows_dir = tmp_path / "TV Shows"
    movies_dir.mkdir(parents=True)
    shows_dir.mkdir(parents=True)

    # 1. Tracked Movie
    tracked_dir = movies_dir / "English" / "Inception (2010)"
    tracked_dir.mkdir(parents=True)
    tracked_strm = tracked_dir / "Inception (2010).strm"
    tracked_strm.write_text("http://127.0.0.1:8080/stream/1:100/video.mp4")

    # 2. Untracked Movie
    untracked_dir = movies_dir / "English" / "Untracked Indie Film (2024)"
    untracked_dir.mkdir(parents=True)
    untracked_strm = untracked_dir / "Untracked Indie Film (2024).strm"
    untracked_strm.write_text("http://127.0.0.1:8080/stream/1:200/video.mp4")

    # 3. User-created sidecars (notes.txt, custom.xml, cover_backup.png)
    user_sidecar_dir = movies_dir / "English" / "User Curated Title (2023)"
    user_sidecar_dir.mkdir(parents=True)
    user_txt = user_sidecar_dir / "notes.txt"
    user_txt.write_text("personal user notes")
    user_xml = user_sidecar_dir / "custom.xml"
    user_xml.write_text("<metadata>personal</metadata>")
    user_png = user_sidecar_dir / "cover_backup.png"
    user_png.write_bytes(b"image bytes")

    # Missing file path (genuine orphan)
    orphan_strm_path = str(movies_dir / "English" / "Ghost (1990)" / "Ghost (1990).strm")

    deleted_item_ids = []
    recorded_reviews = []

    class MockResponse:
        def __init__(self, data=None, status=200):
            self.data = data or {}
            self.status = status
        async def json(self):
            return self.data
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    jellyfin_items = [
        # Tracked movie duplicate 1
        {"Id": "dup-item-1", "Name": "Inception", "Type": "Movie", "Path": str(tracked_strm), "ProviderIds": {"Tmdb": "27205"}},
        # Tracked movie duplicate 2
        {"Id": "dup-item-2", "Name": "Inception", "Type": "Movie", "Path": str(tracked_strm), "ProviderIds": {"Tmdb": "27205"}},
        # Untracked movie item in Jellyfin
        {"Id": "untracked-item-1", "Name": "Untracked Indie Film", "Type": "Movie", "Path": str(untracked_strm), "ProviderIds": {}},
        # Genuine orphan item (missing from disk)
        {"Id": "orphan-item-ghost", "Name": "Ghost", "Type": "Movie", "Path": orphan_strm_path, "ProviderIds": {}},
    ]

    class MockSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def get(self, url, **kwargs):
            return MockResponse({"Items": jellyfin_items}, status=200)
        def delete(self, url, **kwargs):
            item_id = url.rsplit("/", 1)[-1]
            deleted_item_ids.append(item_id)
            return MockResponse(status=204)

    class MockAiohttp:
        class TCPConnector:
            def __init__(self, *args, **kwargs):
                pass
        def ClientSession(self, *args, **kwargs):
            return MockSession()

    class MockJob:
        payload = {"strm_path": str(tracked_strm), "target_dir": str(tracked_dir)}

    class MockRecord:
        source_chat_id = -100
        source_message_id = 100

    class MockCatalog:
        def list_records(self, include_deleted=False):
            return [MockRecord()]
        def find_job(self, chat_id, msg_id):
            return MockJob()

    async def mock_async_noop(*args, **kwargs):
        pass

    import socket
    ns = {
        "logger": type("_Sink", (), {"info": lambda *a, **k: None, "warning": lambda *a, **k: None})(),
        "os": os,
        "re": re,
        "socket": socket,
        "aiohttp": MockAiohttp(),
        "asyncio": asyncio,
        "_jellyfin_auth_header": lambda: {"X-Emby-Token": "test-token"},
        "_read_jellyfin_api_key": lambda: "test-token",
        "MOVIES_DIR": str(movies_dir),
        "SHOWS_DIR": str(shows_dir),
        "FILE_ID_CACHE": {},
        "MEDIA_CATALOG": MockCatalog(),
        "is_blacklisted": lambda text: False,
        "_record_review_job": lambda data, confidence=0.0, reason="": recorded_reviews.append((data, confidence, reason)),
        "save_cache_async": mock_async_noop,
        "deduplicate_jellyfin_items": mock_async_noop,
        "refresh_jellyfin_item_by_path": mock_async_noop,
        "request_jellyfin_scan": mock_async_noop,
        "parse_media_meta": lambda name: {"title": name, "clean_title": name},
        "_canonical_media_key": lambda e: f"name:{e.get('title') or e.get('clean_title') or 'unknown'}",
        "DEFAULT_LANGUAGE": "English",
    }

    # Step A: Run deduplicate_jellyfin_items
    exec(compile(ast.Module([dedup_node], []), "<ast>", "exec"), ns)
    dedup_func = ns["deduplicate_jellyfin_items"]
    asyncio.run(dedup_func())

    # Verify deduplication results:
    # 1. Genuine orphan is deleted
    assert "orphan-item-ghost" in deleted_item_ids
    # 2. Exactly one Inception duplicate is deleted
    assert ("dup-item-1" in deleted_item_ids) ^ ("dup-item-2" in deleted_item_ids)
    # 3. Untracked indie film (exists on disk) is NOT deleted
    assert "untracked-item-1" not in deleted_item_ids

    # Step B: Run reindex_all_with_imdb
    exec(compile(ast.Module([reindex_node], []), "<ast>", "exec"), ns)
    reindex_func = ns["reindex_all_with_imdb"]
    asyncio.run(reindex_func(force_all=False))

    # Verify filesystem and review queue results:
    # 1. Tracked movie survives
    assert tracked_dir.exists()
    assert tracked_strm.exists()

    # 2. Untracked movie survives on disk and is queued for review
    assert untracked_dir.exists()
    assert untracked_strm.exists()

    # 3. User sidecars survive on disk and are queued for review
    assert user_sidecar_dir.exists()
    assert user_txt.exists()
    assert user_xml.exists()
    assert user_png.exists()

    reasons = [r[2] for r in recorded_reviews]
    assert any("untracked_strm_file:Untracked Indie Film (2024).strm" in r for r in reasons)
    assert any("untracked_directory:User Curated Title (2023)" in r for r in reasons)


def test_folder_name_idempotent_single_year():
    from apex_stream.organize import folder_name, strm_base
    assert folder_name("Iron Man 3 (2013)", 2013) == "Iron Man 3 (2013)"
    assert folder_name("Iron Man 3 (2013) (2013)", 2013) == "Iron Man 3 (2013)"
    assert folder_name("Ne Zha (2019) (2019)", 2019) == "Ne Zha (2019)"
    assert folder_name("Ne Zha (2019)", 2019) == "Ne Zha (2019)"
    assert folder_name("Khalifa (2026)", 2026) == "Khalifa (2026)"
    assert folder_name("Khalifa (2026) (2026)", 2026) == "Khalifa (2026)"
    # TV episode filename base with hyphen
    tv_entry = {"clean_title": "Wednesday", "year": 2024, "is_tv": True, "season": 2, "episode": 1}
    assert strm_base(tv_entry) == "Wednesday - S02E01"
    tv_entry2 = {"title": "Wednesday (2024)", "year": 2024, "is_tv": True, "season": 2, "episode": 8}
    assert strm_base(tv_entry2) == "Wednesday - S02E08"


def test_clean_search_title_strips_leading_episode():
    from apex_stream.tmdb_meta import _clean_search_title, _search_candidates
    assert _clean_search_title("Episode 3 - Green Lantern (2011)") == "green lantern"
    cands = _search_candidates("Episode 3 - Green Lantern (2011)")
    assert "green lantern" in cands


def test_deduplicate_jellyfin_items_preserves_conflicting_paths(tmp_path):
    movies_dir = tmp_path / "Movies"
    path1 = movies_dir / "English" / "Ne Zha (2019)" / "Ne Zha (2019).strm"
    path2 = movies_dir / "English" / "Ne Zha (2019) (2019)" / "Ne Zha (2019) (2019).strm"
    path1.parent.mkdir(parents=True, exist_ok=True)
    path2.parent.mkdir(parents=True, exist_ok=True)
    path1.write_text("stream-url-1")
    path2.write_text("stream-url-2")

    items = [
        {"Id": "item-1", "Name": "Ne Zha", "Type": "Movie", "Path": str(path1), "ProviderIds": {"Tmdb": "615453"}},
        {"Id": "item-2", "Name": "Ne Zha", "Type": "Movie", "Path": str(path2), "ProviderIds": {"Tmdb": "615453"}},
    ]

    deleted_item_ids = []
    recorded_reviews = []

    class MockResponse:
        def __init__(self, status=200, json_data=None):
            self.status = status
            self._json = json_data or {}
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def json(self): return self._json

    class MockSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def get(self, url, params=None, headers=None, timeout=None):
            return MockResponse(200, {"Items": items})
        def delete(self, url, params=None, headers=None, timeout=None):
            item_id = url.rstrip("/").split("/")[-1]
            deleted_item_ids.append(item_id)
            return MockResponse(204)

    class MockAiohttp:
        ClientSession = MockSession

    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    tag_node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "extract_version_tag")
    node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "deduplicate_jellyfin_items")

    ns = {
        "logger": type("_Sink", (), {"info": lambda *a, **k: None, "warning": lambda *a, **k: None})(),
        "os": os,
        "re": re,
        "shutil": shutil,
        "aiohttp": MockAiohttp(),
        "_jellyfin_auth_header": lambda: {"X-Emby-Token": "test-token"},
        "_read_jellyfin_api_key": lambda: "test-token",
        "MOVIES_DIR": str(movies_dir),
        "SHOWS_DIR": str(tmp_path / "TV Shows"),
        "FILE_ID_CACHE": {},
        "is_blacklisted": lambda text: False,
        "_record_review_job": lambda data, confidence=0.0, reason="": recorded_reviews.append((data, confidence, reason)),
        "parse_media_meta": lambda name: {"title": name, "clean_title": name},
        "_canonical_media_key": lambda e: "tmdb:615453",
        "DEFAULT_LANGUAGE": "English",
        "detect_language": lambda t: "English",
        "_save_cache_sync": lambda: None,
    }
    exec(compile(ast.Module([tag_node, node], []), "<ast>", "exec"), ns)
    dedup = ns["deduplicate_jellyfin_items"]

    res = asyncio.run(dedup())

    # Neither existing path should be deleted from Jellyfin!
    assert "item-1" not in deleted_item_ids
    assert "item-2" not in deleted_item_ids
    assert len(deleted_item_ids) == 0

    # Conflicting existing paths must be sent to review
    assert any("conflicting_identity_path:tmdb:615453" in r[2] for r in recorded_reviews)


def test_deduplicate_jellyfin_items_passes_delete_file_false(tmp_path):
    movies_dir = tmp_path / "Movies"
    ghost_strm = movies_dir / "Malayalam" / "Ghost (2026)" / "Ghost (2026).strm"

    items = [
        {
            "Id": "ghost-item-1",
            "Name": "Ghost (2026)",
            "Path": str(ghost_strm),
            "Type": "Movie",
            "ProviderIds": {"Tmdb": "1036081"},
        }
    ]

    deleted_params = []

    class MockResponse:
        def __init__(self, status=200, json_data=None):
            self.status = status
            self._json = json_data or {}
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def json(self): return self._json

    class MockSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def get(self, url, params=None, headers=None, timeout=None):
            return MockResponse(200, {"Items": items})
        def delete(self, url, params=None, headers=None, timeout=None):
            deleted_params.append(params)
            return MockResponse(204)

    class MockAiohttp:
        ClientSession = MockSession

    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "deduplicate_jellyfin_items")

    ns = {
        "logger": type("_Sink", (), {"info": lambda *a, **k: None, "warning": lambda *a, **k: None})(),
        "os": os,
        "re": re,
        "shutil": shutil,
        "aiohttp": MockAiohttp(),
        "_jellyfin_auth_header": lambda: {"X-Emby-Token": "test-token"},
        "_read_jellyfin_api_key": lambda: "test-token",
        "MOVIES_DIR": str(movies_dir),
        "SHOWS_DIR": str(tmp_path / "TV Shows"),
        "FILE_ID_CACHE": {},
        "is_blacklisted": lambda text: False,
        "_record_review_job": lambda data, confidence=0.0, reason="": None,
        "parse_media_meta": lambda name: {"title": name, "clean_title": name},
        "_canonical_media_key": lambda e: "tmdb:1036081",
        "DEFAULT_LANGUAGE": "Malayalam",
        "detect_language": lambda t: "Malayalam",
        "extract_version_tag": lambda p: "Alt",
        "_save_cache_sync": lambda: None,
    }
    exec(compile(ast.Module([node], []), "<ast>", "exec"), ns)
    dedup = ns["deduplicate_jellyfin_items"]

    res = asyncio.run(dedup())

    assert len(deleted_params) == 1
    assert deleted_params[0].get("deleteFile") == "false"



def test_migrate_double_year_folders(tmp_path):
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "migrate_double_year_folders")

    movies = tmp_path / "Movies"
    double_year_dir = movies / "Chinese" / "Ne Zha (2019) (2019)"
    double_year_dir.mkdir(parents=True)
    strm = double_year_dir / "Ne Zha (2019) (2019).strm"
    strm.write_text("http://127.0.0.1:8080/stream/1:2/video.mp4")
    nfo = double_year_dir / "movie.nfo"
    nfo.write_text("<movie><title>Ne Zha</title></movie>")

    cache = {
        "1:2": {
            "title": "Ne Zha",
            "year": 2019,
            "target_dir": str(double_year_dir),
            "strm_path": str(strm),
        }
    }

    ns = {
        "logger": type("_Sink", (), {"info": lambda *a, **k: None, "warning": lambda *a, **k: None})(),
        "os": os,
        "re": re,
        "shutil": shutil,
        "FILE_ID_CACHE": cache,
        "_save_cache_sync": lambda: None,
    }
    exec(compile(ast.Module([node], []), "<ast>", "exec"), ns)
    migrate = ns["migrate_double_year_folders"]

    migrate(str(movies))

    canonical_dir = movies / "Chinese" / "Ne Zha (2019)"
    assert canonical_dir.exists()
    assert (canonical_dir / "Ne Zha (2019).strm").exists()
    assert (canonical_dir / "movie.nfo").exists()
    assert not double_year_dir.exists()
    assert cache["1:2"]["target_dir"] == str(canonical_dir)
    assert cache["1:2"]["strm_path"] == str(canonical_dir / "Ne Zha (2019).strm")


def test_episode_identity_never_merges_different_episodes(tmp_path):
    shows_dir = tmp_path / "TV Shows"
    show_dir = shows_dir / "Loki (2021)" / "Season 02"
    show_dir.mkdir(parents=True)
    ep1_strm = show_dir / "Loki - S02E01.strm"
    ep2_strm = show_dir / "Loki - S02E02.strm"
    ep1_strm.write_text("http://127.0.0.1:8080/stream/1:1/video.mp4")
    ep2_strm.write_text("http://127.0.0.1:8080/stream/1:2/video.mp4")

    # Both episodes share the same Series TMDB ID in Jellyfin
    items = [
        {"Id": "ep-1", "Name": "Ouroboros", "Type": "Episode", "Path": str(ep1_strm), "SeriesName": "Loki", "ParentIndexNumber": 2, "IndexNumber": 1, "ProviderIds": {"Tmdb": "114472"}},
        {"Id": "ep-2", "Name": "Breaking Brad", "Type": "Episode", "Path": str(ep2_strm), "SeriesName": "Loki", "ParentIndexNumber": 2, "IndexNumber": 2, "ProviderIds": {"Tmdb": "114472"}},
    ]

    deleted_item_ids = []

    class MockResponse:
        def __init__(self, status=200, json_data=None):
            self.status = status
            self._json = json_data or {}
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def json(self): return self._json

    class MockSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def get(self, url, params=None, headers=None, timeout=None):
            return MockResponse(200, {"Items": items})
        def delete(self, url, params=None, headers=None, timeout=None):
            deleted_item_ids.append(url.rstrip("/").split("/")[-1])
            return MockResponse(204)

    class MockAiohttp:
        ClientSession = MockSession

    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "deduplicate_jellyfin_items")

    ns = {
        "logger": type("_Sink", (), {"info": lambda *a, **k: None, "warning": lambda *a, **k: None})(),
        "os": os,
        "re": re,
        "shutil": shutil,
        "aiohttp": MockAiohttp(),
        "_jellyfin_auth_header": lambda: {"X-Emby-Token": "test-token"},
        "_read_jellyfin_api_key": lambda: "test-token",
        "MOVIES_DIR": str(tmp_path / "Movies"),
        "SHOWS_DIR": str(shows_dir),
        "FILE_ID_CACHE": {},
        "is_blacklisted": lambda text: False,
        "_record_review_job": lambda data, confidence=0.0, reason="": None,
        "parse_media_meta": lambda name: {"show_name": "Loki", "season": 2, "episode": 1 if "E01" in name else 2, "is_tv": True},
        "_canonical_media_key": lambda e: "episode:loki",
        "DEFAULT_LANGUAGE": "English",
        "detect_language": lambda t: "English",
        "extract_version_tag": lambda p: "Alt",
        "_save_cache_sync": lambda: None,
    }
    exec(compile(ast.Module([node], []), "<ast>", "exec"), ns)
    dedup = ns["deduplicate_jellyfin_items"]

    res = asyncio.run(dedup())

    # Neither episode should EVER be deleted!
    assert "ep-1" not in deleted_item_ids
    assert "ep-2" not in deleted_item_ids
    assert len(deleted_item_ids) == 0


def test_movie_identity_distinguishes_sequels(tmp_path):
    movies_dir = tmp_path / "Movies"
    m1_dir = movies_dir / "English" / "The Bad Guys (2022)"
    m2_dir = movies_dir / "English" / "The Bad Guys 2 (2025)"
    m1_dir.mkdir(parents=True)
    m2_dir.mkdir(parents=True)
    m1_strm = m1_dir / "The Bad Guys (2022).strm"
    m2_strm = m2_dir / "The Bad Guys 2 (2025).strm"
    m1_strm.write_text("http://127.0.0.1:8080/stream/1:1/video.mp4")
    m2_strm.write_text("http://127.0.0.1:8080/stream/1:2/video.mp4")

    items = [
        {"Id": "bg-1", "Name": "The Bad Guys", "Type": "Movie", "Path": str(m1_strm), "ProductionYear": 2022, "ProviderIds": {"Tmdb": "629542"}},
        {"Id": "bg-2", "Name": "The Bad Guys 2", "Type": "Movie", "Path": str(m2_strm), "ProductionYear": 2025, "ProviderIds": {"Tmdb": "1182390"}},
    ]

    deleted_item_ids = []

    class MockResponse:
        def __init__(self, status=200, json_data=None):
            self.status = status
            self._json = json_data or {}
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def json(self): return self._json

    class MockSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def get(self, url, params=None, headers=None, timeout=None):
            return MockResponse(200, {"Items": items})
        def delete(self, url, params=None, headers=None, timeout=None):
            deleted_item_ids.append(url.rstrip("/").split("/")[-1])
            return MockResponse(204)

    class MockAiohttp:
        ClientSession = MockSession

    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "deduplicate_jellyfin_items")

    ns = {
        "logger": type("_Sink", (), {"info": lambda *a, **k: None, "warning": lambda *a, **k: None})(),
        "os": os,
        "re": re,
        "shutil": shutil,
        "aiohttp": MockAiohttp(),
        "_jellyfin_auth_header": lambda: {"X-Emby-Token": "test-token"},
        "_read_jellyfin_api_key": lambda: "test-token",
        "MOVIES_DIR": str(movies_dir),
        "SHOWS_DIR": str(tmp_path / "TV Shows"),
        "FILE_ID_CACHE": {},
        "is_blacklisted": lambda text: False,
        "_record_review_job": lambda data, confidence=0.0, reason="": None,
        "parse_media_meta": lambda name: {"clean_title": name, "year": 2022 if "2022" in name else 2025},
        "_canonical_media_key": lambda e: "movie:bad-guys",
        "DEFAULT_LANGUAGE": "English",
        "detect_language": lambda t: "English",
        "_save_cache_sync": lambda: None,
    }
    tag_node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "extract_version_tag")
    exec(compile(ast.Module([tag_node, node], []), "<ast>", "exec"), ns)
    dedup = ns["deduplicate_jellyfin_items"]

    res = asyncio.run(dedup())

    # Neither sequel should be deleted
    assert "bg-1" not in deleted_item_ids
    assert "bg-2" not in deleted_item_ids
    assert len(deleted_item_ids) == 0


def test_same_movie_multiple_releases_consolidated_as_alternate_versions(tmp_path):
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    tag_node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "extract_version_tag")
    dedup_node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "deduplicate_jellyfin_items")

    movies_dir = tmp_path / "Movies"
    canon_dir = movies_dir / "English" / "Spider-Man Homecoming (2017)"
    alt_dir = movies_dir / "English" / "Spiderman Homecoming (2017)"
    canon_dir.mkdir(parents=True)
    alt_dir.mkdir(parents=True)

    canon_strm = canon_dir / "Spider-Man Homecoming (2017).strm"
    alt_strm = alt_dir / "Spider-Man.Homecoming.2017.720p.WEB-DL.strm"
    canon_strm.write_text("http://127.0.0.1:8080/stream/1:100/video.mp4")
    alt_strm.write_text("http://127.0.0.1:8080/stream/1:200/video.mp4")

    cache = {
        "1:100": {"strm_path": str(canon_strm), "target_dir": str(canon_dir)},
        "1:200": {"strm_path": str(alt_strm), "target_dir": str(alt_dir)},
    }

    items = [
        {"Id": "sm-1", "Name": "Spider-Man: Homecoming", "Type": "Movie", "Path": str(canon_strm), "ProviderIds": {"Tmdb": "315635"}},
        {"Id": "sm-2", "Name": "Spider-Man: Homecoming", "Type": "Movie", "Path": str(alt_strm), "ProviderIds": {"Tmdb": "315635"}},
    ]

    deleted_item_ids = []

    class MockResponse:
        def __init__(self, status=200, json_data=None):
            self.status = status
            self._json = json_data or {}
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def json(self): return self._json

    class MockSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def get(self, url, params=None, headers=None, timeout=None):
            return MockResponse(200, {"Items": items})
        def delete(self, url, params=None, headers=None, timeout=None):
            deleted_item_ids.append(url.rstrip("/").split("/")[-1])
            return MockResponse(204)

    class MockAiohttp:
        ClientSession = MockSession

    ns = {
        "logger": type("_Sink", (), {"info": lambda *a, **k: None, "warning": lambda *a, **k: None})(),
        "os": os,
        "re": re,
        "shutil": shutil,
        "aiohttp": MockAiohttp(),
        "_jellyfin_auth_header": lambda: {"X-Emby-Token": "test-token"},
        "_read_jellyfin_api_key": lambda: "test-token",
        "MOVIES_DIR": str(movies_dir),
        "SHOWS_DIR": str(tmp_path / "TV Shows"),
        "FILE_ID_CACHE": cache,
        "is_blacklisted": lambda text: False,
        "_record_review_job": lambda data, confidence=0.0, reason="": None,
        "parse_media_meta": lambda name: {"clean_title": "Spider-Man Homecoming", "year": 2017},
        "_canonical_media_key": lambda e: "movie:spiderman",
        "DEFAULT_LANGUAGE": "English",
        "detect_language": lambda t: "English",
        "_save_cache_sync": lambda: None,
    }
    exec(compile(ast.Module([tag_node, dedup_node], []), "<ast>", "exec"), ns)
    dedup = ns["deduplicate_jellyfin_items"]

    res = asyncio.run(dedup())

    # Alternate release must be consolidated into the canonical folder as an alternate version!
    consolidated_strm = canon_dir / "Spider-Man Homecoming (2017) - 720P WEB-DL.strm"
    assert canon_strm.exists()
    assert consolidated_strm.exists()
    assert not alt_dir.exists()
    assert cache["1:200"]["target_dir"] == str(canon_dir)
    assert cache["1:200"]["strm_path"] == str(consolidated_strm)
    assert len(deleted_item_ids) == 0


def _extract_top_level_function(name, extra_ns=None):
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    ns = {
        "os": os,
        "shutil": shutil,
        "uuid": uuid,
        "Dict": Dict,
        "Optional": Optional,
        "logger": type(
            "_Sink",
            (),
            {
                "info": lambda *a, **k: None,
                "warning": lambda *a, **k: None,
                "error": lambda *a, **k: None,
            },
        )(),
        "_record_review_job": lambda *a, **k: None,
    }
    if extra_ns:
        ns.update(extra_ns)
    exec(compile(ast.Module([node], []), "<ast>", "exec"), ns)
    return ns[name]


def test_write_text_if_changed_is_idempotent(tmp_path):
    import uuid

    write_if_changed = _extract_top_level_function("_write_text_if_changed")
    path = tmp_path / "movie.nfo"

    assert write_if_changed(str(path), "<movie>correct</movie>") is True
    first_bytes = path.read_bytes()
    first_mtime = path.stat().st_mtime_ns

    assert write_if_changed(str(path), "<movie>correct</movie>") is False
    assert path.read_bytes() == first_bytes
    assert path.stat().st_mtime_ns == first_mtime
    assert not list(tmp_path.glob("*.tmp.*"))


def test_release_group_prefix_is_removed_only_from_matching_copy():
    strip_prefix = _extract_top_level_function("_strip_known_release_group_prefix")
    assert strip_prefix("@WMR_SpiderMan.Homecoming.2017.720p.mkv") == "Spider Man.Homecoming.2017.720p.mkv"
    assert strip_prefix("@WMR Resident Evil Apocalypse (2004).mkv") == "Resident Evil Apocalypse (2004).mkv"
    assert strip_prefix("Spider-Man: Homecoming (2017).mkv") == "Spider-Man: Homecoming (2017).mkv"


def test_legacy_folder_migration_preserves_sidecars_and_conflicts(tmp_path):
    move_contents = _extract_top_level_function("_move_legacy_folder_contents_preserving_conflicts")
    source = tmp_path / "Tamil" / "Aadu 3 (2026) (2026)"
    target = tmp_path / "Malayalam" / "Aadu 3 Part 1 (2026)"
    source.mkdir(parents=True)
    target.mkdir(parents=True)

    (source / "Aadu 3 (2026) (2026).strm").write_text("http://stream/46", encoding="utf-8")
    (source / "movie.nfo").write_text("<movie>verified Aadu metadata</movie>", encoding="utf-8")
    (source / "poster.jpg").write_bytes(b"legacy-poster")
    (target / "poster.jpg").write_bytes(b"canonical-poster")

    moved = move_contents(str(source), str(target), preferred_strm_name="Aadu 3 Part 1 (2026).strm")

    assert (target / "Aadu 3 Part 1 (2026).strm").read_text(encoding="utf-8") == "http://stream/46"
    assert (target / "movie.nfo").read_text(encoding="utf-8") == "<movie>verified Aadu metadata</movie>"
    assert (target / "poster.jpg").read_bytes() == b"canonical-poster"
    # Conflicting artwork is preserved rather than deleted or overwritten.
    assert (source / "poster.jpg").read_bytes() == b"legacy-poster"
    assert str(source / "Aadu 3 (2026) (2026).strm") in moved
    assert source.exists()


def test_refresh_uses_explicit_legacy_path_and_verifies_identity(tmp_path):
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    node = next(
        n for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "refresh_jellyfin_item_by_path"
    )

    target_path = tmp_path / "Movies" / "Malayalam" / "Aadu 3 Part 1 (2026)" / "Aadu 3 Part 1 (2026).strm"
    legacy_path = tmp_path / "Movies" / "Tamil" / "Aadu 3 (2026) (2026)" / "Aadu 3 (2026) (2026).strm"
    target_path.parent.mkdir(parents=True)
    legacy_path.parent.mkdir(parents=True)
    target_path.write_text("http://stream/46", encoding="utf-8")
    legacy_path.write_text("http://stream/46", encoding="utf-8")

    jellyfin_items = [{
        "Id": "aadu-item-123",
        "Name": "Karuppu Aadu",
        "Type": "Movie",
        "Path": str(legacy_path),
        "ProductionYear": 2020,
        "ProviderIds": {"Tmdb": "1715385", "Imdb": "tt8142672"},
    }]
    refreshed_item = {
        "Id": "aadu-item-123",
        "Name": "Aadu 3",
        "Type": "Movie",
        "Path": str(legacy_path),
        "ProductionYear": 2026,
        "ProviderIds": {"Tmdb": "1715385", "Imdb": "tt8142672"},
    }
    captured_post_calls = []

    class MockResponse:
        def __init__(self, status=200, json_data=None):
            self.status = status
            self._json = json_data or {}
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def json(self): return self._json

    class MockSession:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def get(self, url, params=None, headers=None, timeout=None):
            if url.endswith("/Items"):
                return MockResponse(200, {"Items": jellyfin_items})
            if url.endswith("/Items/aadu-item-123"):
                return MockResponse(200, refreshed_item)
            return MockResponse(404, {})
        def post(self, url, params=None, headers=None, timeout=None):
            captured_post_calls.append({"url": url, "params": params})
            return MockResponse(204)

    class MockAiohttp:
        class TCPConnector:
            def __init__(self, *a, **k): pass
        ClientSession = MockSession

    import socket
    ns = {
        "logger": type(
            "_Sink",
            (),
            {
                "info": lambda *a, **k: None,
                "warning": lambda *a, **k: None,
                "error": lambda *a, **k: None,
            },
        )(),
        "os": os,
        "re": re,
        "socket": socket,
        "aiohttp": MockAiohttp(),
        "asyncio": asyncio,
        "_jellyfin_auth_header": lambda: {"X-Emby-Token": "test-token"},
        "_read_jellyfin_api_key": lambda: "test-token",
        "_record_review_job": lambda *a, **k: None,
    }
    exec(compile(ast.Module([node], []), "<ast>", "exec"), ns)
    refresh_func = ns["refresh_jellyfin_item_by_path"]

    ok = asyncio.run(
        refresh_func(
            str(target_path),
            alt_paths=[str(legacy_path)],
            retries=1,
            delay=0,
            expected_title="Aadu 3",
            expected_year=2026,
            expected_tmdb_id=1715385,
            expected_imdb_id="tt8142672",
        )
    )

    assert ok is True
    assert len(captured_post_calls) == 1  # never refresh SeasonId/SeriesId for an episode item
    post_call = captured_post_calls[0]
    assert "aadu-item-123" in post_call["url"]
    assert post_call["params"].get("metadataRefreshMode") == "FullRefresh"
    assert post_call["params"].get("replaceAllMetadata") == "true"
    assert post_call["params"].get("replaceAllImages") == "false"


def test_refresh_refuses_basename_only_match_for_an_unrelated_path(tmp_path):
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    node = next(
        n for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "refresh_jellyfin_item_by_path"
    )
    target_path = tmp_path / "Movies" / "English" / "Spider-Man Homecoming (2017)" / "Spider-Man Homecoming (2017).strm"
    unrelated_path = tmp_path / "Movies" / "Other" / "Spider-Man Homecoming (2017)" / "Spider-Man Homecoming (2017).strm"
    target_path.parent.mkdir(parents=True)
    unrelated_path.parent.mkdir(parents=True)
    target_path.write_text("http://stream/target", encoding="utf-8")
    unrelated_path.write_text("http://stream/unrelated", encoding="utf-8")
    jellyfin_items = [{
        "Id": "wrong-item-id",
        "Name": "Spider-Man: Homecoming",
        "Type": "Movie",
        "Path": str(unrelated_path),
        "ProductionYear": 2017,
        "ProviderIds": {"Imdb": "tt2250912"},
    }]
    posted = []

    class MockResponse:
        def __init__(self, status=200, json_data=None):
            self.status = status
            self._json = json_data or {}
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def json(self): return self._json

    class MockSession:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def get(self, url, params=None, headers=None, timeout=None):
            return MockResponse(200, {"Items": jellyfin_items})
        def post(self, url, params=None, headers=None, timeout=None):
            posted.append(url)
            return MockResponse(204)

    class MockAiohttp:
        class TCPConnector:
            def __init__(self, *a, **k): pass
        ClientSession = MockSession

    import socket
    ns = {
        "logger": type("_Sink", (), {"info": lambda *a, **k: None, "warning": lambda *a, **k: None, "error": lambda *a, **k: None})(),
        "os": os,
        "re": re,
        "socket": socket,
        "aiohttp": MockAiohttp(),
        "asyncio": asyncio,
        "_jellyfin_auth_header": lambda: {"X-Emby-Token": "test-token"},
        "_read_jellyfin_api_key": lambda: "test-token",
    }
    exec(compile(ast.Module([node], []), "<ast>", "exec"), ns)
    ok = asyncio.run(ns["refresh_jellyfin_item_by_path"](str(target_path), retries=1, delay=0))

    assert ok is False
    assert posted == []


def test_choose_safe_strm_path_preserves_existing_source_and_reuses_its_variant(tmp_path):
    choose = _extract_top_level_function(
        "_choose_safe_strm_path",
        {
            "extract_version_tag": lambda path: "720P WEB-DL" if "720p" in str(path).lower() else "",
            "_stream_source_identity": lambda value: tuple(re.search(r"/stream/(-?\d+)/(\d+)(?:/|$)", str(value or "")).groups()) if re.search(r"/stream/(-?\d+)/(\d+)(?:/|$)", str(value or "")) else None,
        },
    )
    canonical = tmp_path / "Movies" / "English" / "Spider-Man Homecoming (2017)" / "Spider-Man Homecoming (2017).strm"
    canonical.parent.mkdir(parents=True)
    original_url = "http://127.0.0.1:8080/stream/-100/101/video.mp4"
    alternate_url = "http://127.0.0.1:8080/stream/-100/202/video.mp4"
    canonical.write_text(original_url, encoding="utf-8")

    entry = {
        "chat_id": -100,
        "msg_id": 202,
        "file_name": "Spider.Man.Homecoming.2017.720p.WEB-DL.x264.mkv",
        "title": "Spider-Man Homecoming",
    }
    base, path, should_write = choose(
        entry, str(canonical.parent), "Spider-Man Homecoming (2017)", alternate_url
    )
    assert path != str(canonical)
    assert base.startswith("Spider-Man Homecoming (2017) - ")
    assert should_write is True
    assert canonical.read_text(encoding="utf-8") == original_url

    Path(path).write_text(alternate_url, encoding="utf-8")
    base2, path2, should_write2 = choose(
        entry, str(canonical.parent), "Spider-Man Homecoming (2017)", alternate_url
    )
    assert base2 == base
    assert path2 == path
    assert should_write2 is False
    assert canonical.read_text(encoding="utf-8") == original_url


def test_refresh_tv_episode_verifies_parent_series_identity(tmp_path):
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    node = next(
        n for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "refresh_jellyfin_item_by_path"
    )
    episode_path = tmp_path / "TV Shows" / "Loki" / "Season 02" / "Loki - S02E01.strm"
    episode_path.parent.mkdir(parents=True)
    episode_path.write_text("http://stream/-100/501", encoding="utf-8")
    items = [{
        "Id": "episode-1",
        "Name": "Some Episode Name",
        "Type": "Episode",
        "Path": str(episode_path),
        "SeriesId": "series-1",
        "ParentIndexNumber": 2,
        "IndexNumber": 1,
        "ProviderIds": {},
    }]
    episode = dict(items[0])
    series = {
        "Id": "series-1",
        "Name": "Loki",
        "ProductionYear": 2023,
        "ProviderIds": {"Tmdb": "84958", "Imdb": "tt9140554"},
    }
    posted = []

    class MockResponse:
        def __init__(self, status=200, json_data=None):
            self.status = status
            self._json = json_data or {}
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def json(self): return self._json

    class MockSession:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def get(self, url, params=None, headers=None, timeout=None):
            if url.endswith("/Items"):
                return MockResponse(200, {"Items": items})
            if url.endswith("/Items/episode-1"):
                return MockResponse(200, episode)
            if url.endswith("/Items/series-1"):
                return MockResponse(200, series)
            return MockResponse(404, {})
        def post(self, url, params=None, headers=None, timeout=None):
            posted.append(url)
            return MockResponse(204)

    class MockAiohttp:
        class TCPConnector:
            def __init__(self, *a, **k): pass
        ClientSession = MockSession

    import socket
    ns = {
        "logger": type("_Sink", (), {
            "info": lambda *a, **k: None,
            "warning": lambda *a, **k: None,
            "error": lambda *a, **k: None,
        })(),
        "os": os,
        "re": re,
        "socket": socket,
        "aiohttp": MockAiohttp(),
        "asyncio": asyncio,
        "_jellyfin_auth_header": lambda: {"X-Emby-Token": "test-token"},
        "_read_jellyfin_api_key": lambda: "test-token",
        "_record_review_job": lambda *a, **k: None,
    }
    exec(compile(ast.Module([node], []), "<ast>", "exec"), ns)
    ok = asyncio.run(
        ns["refresh_jellyfin_item_by_path"](
            str(episode_path),
            retries=1,
            delay=0,
            expected_title="Loki",
            expected_year=2023,
            expected_tmdb_id=84958,
            expected_imdb_id="tt9140554",
            expected_is_tv=True,
            expected_season=2,
            expected_episode=1,
        )
    )
    assert ok is True
    assert posted == ["http://127.0.0.1:8096/Items/episode-1/Refresh"]
