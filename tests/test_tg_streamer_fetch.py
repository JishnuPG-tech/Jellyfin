"""Regression tests for tg_streamer._fetch_chunks short-run + reference refresh.

tg_streamer imports pyrogram which isn't installed locally, so the functions
under test are extracted from the source AST and executed against fakes.
"""

import ast
import sys
import types
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "tg_streamer.py"

# Stub pyrogram.raw so AST-extracted _warm_channel_peer / _media_session_for /
# _stream_file_chunks can resolve raw.functions.* / raw.types.* without the
# real package installed.
_pg = types.ModuleType("pyrogram")
_raw = types.ModuleType("pyrogram.raw")
_funcs = types.ModuleType("pyrogram.raw.functions")
_channels = types.ModuleType("pyrogram.raw.functions.channels")
_auth_funcs = types.ModuleType("pyrogram.raw.functions.auth")
_upload_funcs = types.ModuleType("pyrogram.raw.functions.upload")
_rtypes = types.ModuleType("pyrogram.raw.types")
_up_types = types.ModuleType("pyrogram.raw.types.upload")
_utils = types.ModuleType("pyrogram.utils")
_crypto = types.ModuleType("pyrogram.crypto")
_aes = types.ModuleType("pyrogram.crypto.aes")
_errors = types.ModuleType("pyrogram.errors")
_session = types.ModuleType("pyrogram.session")
_file_id = types.ModuleType("pyrogram.file_id")


class _Stub:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _UploadFile(_Stub):
    pass


class _FileCdnRedirect(_Stub):
    pass


class _GetFile(_Stub):
    pass


class _InputDocumentFileLocation(_Stub):
    pass


class _GetChannels(_Stub):
    pass


class _InputChannel(_Stub):
    pass


class _ExportAuthorization(_Stub):
    pass


class _ImportAuthorization(_Stub):
    pass


class FloodWait(Exception):
    def __init__(self, value):
        super().__init__(value)
        self.value = value


class AuthBytesInvalid(Exception):
    pass


class VolumeLocNotFound(Exception):
    pass


class CDNFileHashMismatch(Exception):
    @staticmethod
    def check(cond, expr):
        if not cond:
            raise CDNFileHashMismatch(expr)


class FileType:
    CHAT_PHOTO = "chat_photo"
    PHOTO = "photo"
    DOCUMENT = "document"


class ThumbnailSource:
    CHAT_PHOTO_BIG = "chat_photo_big"


class _FakeAuth:
    def __init__(self, *a, **k):
        pass

    async def create(self):
        return b"authkey"


class FakeMediaSession:
    instances = []

    def __init__(self, client, dc_id, auth_key, test_mode, **kw):
        self.client = client
        self.dc_id = dc_id
        self.started = False
        self.stopped = False
        self.invoked = []
        FakeMediaSession.instances.append(self)

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    async def invoke(self, rpc, **kw):
        self.invoked.append((rpc, kw))
        if isinstance(rpc, _GetFile):
            return _UploadFile(bytes=b"x" * MIB)
        return _Stub(id=7, bytes=b"import-bytes")


class _FakeFileId:
    @staticmethod
    def decode(file_id):
        return _Stub(
            file_type=FileType.DOCUMENT,
            dc_id=5,
            chat_id=-1003907801136,
            chat_access_hash=0,
            media_id=12345,
            access_hash=67890,
            file_reference=b"ref",
            thumbnail_size="",
            thumbnail_source=None,
        )


def _get_channel_id(chat_id):
    return -chat_id


def ctr256_decrypt(chunk, key, iv):
    return chunk


_up_types.File = _UploadFile
_up_types.FileCdnRedirect = _FileCdnRedirect
_upload_funcs.GetFile = _GetFile
_auth_funcs.ExportAuthorization = _ExportAuthorization
_auth_funcs.ImportAuthorization = _ImportAuthorization
_channels.GetChannels = _GetChannels
_rtypes.InputChannel = _InputChannel
_rtypes.InputDocumentFileLocation = _InputDocumentFileLocation
_funcs.channels = _channels
_funcs.auth = _auth_funcs
_funcs.upload = _upload_funcs
_raw.functions = _funcs
_raw.types = _rtypes
_rtypes.upload = _up_types
_pg.raw = _raw
_utils.get_channel_id = _get_channel_id
_crypto.aes = _aes
_aes.ctr256_decrypt = ctr256_decrypt
_errors.FloodWait = FloodWait
_errors.AuthBytesInvalid = AuthBytesInvalid
_errors.VolumeLocNotFound = VolumeLocNotFound
_errors.CDNFileHashMismatch = CDNFileHashMismatch
_session.Auth = _FakeAuth
_session.Session = FakeMediaSession
_file_id.FileId = _FakeFileId
_file_id.FileType = FileType
_file_id.ThumbnailSource = ThumbnailSource

sys.modules["pyrogram"] = _pg
sys.modules["pyrogram.raw"] = _raw
sys.modules["pyrogram.raw.functions"] = _funcs
sys.modules["pyrogram.raw.functions.channels"] = _channels
sys.modules["pyrogram.raw.functions.auth"] = _auth_funcs
sys.modules["pyrogram.raw.functions.upload"] = _upload_funcs
sys.modules["pyrogram.raw.types"] = _rtypes
sys.modules["pyrogram.raw.types.upload"] = _up_types
sys.modules["pyrogram.utils"] = _utils
sys.modules["pyrogram.crypto"] = _crypto
sys.modules["pyrogram.crypto.aes"] = _aes
sys.modules["pyrogram.errors"] = _errors
sys.modules["pyrogram.session"] = _session
sys.modules["pyrogram.file_id"] = _file_id

MIB = 1024 * 1024
FILE_SIZE = 895_849_434
TAIL_EXPECT = FILE_SIZE - 848 * MIB  # 6,656,986


class RPCError(Exception):
    pass


class FileReferenceExpired(RPCError):
    ID = "FILE_REFERENCE_EXPIRED"


class _Sink:
    def warning(self, message):
        pass

    def info(self, message):
        pass


@pytest.fixture()
def tc():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    names = {
        "_fetch_chunks",
        "_refresh_file_reference",
        "_warm_channel_peer",
        "_cache_lookup",
        "_cache_key",
        "_is_expired_reference",
        "_media_session_for",
        "_stream_file_chunks",
    }
    mod_nodes = [
        n
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names
    ]
    ns = {
        "RPCError": RPCError,
        "FileReferenceExpired": FileReferenceExpired,
        "logger": _Sink(),
        "FILE_ID_CACHE": {},
        "SourceReferenceExpired": type("SourceReferenceExpired", (Exception,), {}),
    }
    exec(compile(ast.Module(mod_nodes, []), "<ast>", "exec"), ns)

    async def save_cache_async():
        pass

    ns["save_cache_async"] = save_cache_async
    return ns


class FakeClient:
    """Pyrogram with an expired reference: first call short/empty, refreshed
    call yields the correct span."""

    def __init__(self, tail_short_by=1083, empty_first=False):
        self.stream_calls = 0
        self.tail_short_by = tail_short_by
        self.empty_first = empty_first
        self.refreshed_id = "NEW:9"

    async def stream_media(self, file_id, offset=0, limit=0):
        self.stream_calls += 1
        start = offset * MIB
        want = limit * MIB
        if self.stream_calls == 1 and file_id.startswith("OLD:"):
            if self.empty_first:
                total = 0
            elif offset == 848:
                total = TAIL_EXPECT - self.tail_short_by
            else:
                total = 0
        else:
            total = min(want, max(FILE_SIZE - start, 0))
        got = 0
        while got < total:
            part = min(MIB, total - got)
            yield b"x" * part
            got += part

    async def get_messages(self, chat_id, message_id):
        size = types.SimpleNamespace(
            video=types.SimpleNamespace(file_id=self.refreshed_id, file_size=FILE_SIZE)
        )
        return size


def _canonical(items):
    """Last-write-wins per offset (mirrors inflight registry dedup)."""
    by_offset = {}
    for offset, chunk in items:
        by_offset[offset] = chunk
    return by_offset


def _run(tc, client, chat, msg, start, count, cache):
    tc["FILE_ID_CACHE"] = dict(cache)

    async def drain():
        out = {}
        async for offset, chunk in tc["_fetch_chunks"](client, chat, msg, start, count):
            out[offset] = chunk
        return out

    return asyncio_run(drain())


import asyncio


def asyncio_run(coro):
    return asyncio.run(coro)


def test_short_tail_refreshes_once_and_serves_full(tc):
    cache = {"-1003907801136:9": {"file_id": "OLD:9", "file_size": FILE_SIZE}}
    out = _run(tc, FakeClient(), -1003907801136, 9, 848, 7, cache)
    total = sum(len(v) for v in out.values())
    assert total == TAIL_EXPECT
    assert len(out) == 7
    assert tc["FILE_ID_CACHE"]["-1003907801136:9"]["file_id"] == "NEW:9"


def test_expired_first_chunk_refreshes(tc):
    cache = {"-1003907801136:9": {"file_id": "OLD:9", "file_size": FILE_SIZE}}
    out = _run(tc, FakeClient(empty_first=True), -1003907801136, 9, 768, 16, cache)
    assert sum(len(v) for v in out.values()) == 16 * MIB
    assert len(out) == 16


def test_healthy_run_does_not_refresh(tc):
    cache = {"-1003907801136:9": {"file_id": "NEW:9", "file_size": FILE_SIZE}}
    client = FakeClient()
    out = _run(tc, client, -1003907801136, 9, 848, 7, cache)
    assert client.stream_calls == 1
    assert sum(len(v) for v in out.values()) == TAIL_EXPECT


def test_refresh_failure_ends_cleanly_no_raise(tc):
    cache = {"-1003907801136:9": {"file_id": "OLD:9", "file_size": FILE_SIZE}}

    class Down(FakeClient):
        async def get_messages(self, chat_id, message_id):
            raise RuntimeError("dc down")

    out = _run(tc, Down(), -1003907801136, 9, 848, 7, cache)
    assert sum(len(v) for v in out.values()) == TAIL_EXPECT - 1083


def test_refresh_peer_warm_retries_on_valueerror(tc):
    """A client with an empty peer cache raises ValueError (Peer id invalid);
    the warm-up resolves the channel and the retried get_messages succeeds."""
    cache = {"-1003907801136:9": {"file_id": "OLD:9", "file_size": FILE_SIZE}}

    class ColdPeer(FakeClient):
        def __init__(self):
            super().__init__()
            self.get_messages_calls = 0
            self.invoked = []

        async def get_messages(self, chat_id, message_id):
            self.get_messages_calls += 1
            if self.get_messages_calls == 1 and not self.invoked:
                raise ValueError(f"Peer id invalid: {chat_id}")
            return await super().get_messages(chat_id, message_id)

        async def invoke(self, rpc, **kwargs):
            self.invoked.append(rpc)
            return types.SimpleNamespace(chats=[object()])

    client = ColdPeer()

    async def drain():
        return await tc["_refresh_file_reference"](client, -1003907801136, 9)

    f_id, f_size = asyncio_run(drain())
    assert f_id == "NEW:9"
    assert f_size == FILE_SIZE
    assert client.get_messages_calls == 2
    assert len(client.invoked) == 1


def test_refresh_peer_warm_failure_returns_none(tc):
    cache = {"-1003907801136:9": {"file_id": "OLD:9", "file_size": FILE_SIZE}}

    class ColdPeerDown(FakeClient):
        def __init__(self):
            super().__init__()
            self.get_messages_calls = 0

        async def get_messages(self, chat_id, message_id):
            self.get_messages_calls += 1
            if self.get_messages_calls == 1:
                raise ValueError(f"Peer id invalid: {chat_id}")
            return await super().get_messages(chat_id, message_id)

        async def invoke(self, rpc, **kwargs):
            raise RuntimeError("GetChannels down")

    client = ColdPeerDown()

    async def drain():
        return await tc["_refresh_file_reference"](client, -1003907801136, 9)

    assert asyncio_run(drain()) is None
    assert client.get_messages_calls == 1


# --- cached media session transport -------------------------------------------------


class MediaStorage:
    def __init__(self, dc_id):
        self._dc = dc_id

    async def dc_id(self):
        return self._dc

    async def test_mode(self):
        return False


class MediaClient:
    """Fake pyrogram client: storage + media_sessions + invoke. Export grants a
    hardcoded peer; GetFile serves `serve_chunks` MiB chunks then one short tail."""

    def __init__(self, primary_dc=5, file_dc=5, serve_chunks=16, fail_export=False):
        self.storage = MediaStorage(primary_dc)
        self.file_dc = file_dc
        self.serve_chunks = serve_chunks
        self.fail_export = fail_export
        self.media_sessions = {}
        self.media_sessions_lock = asyncio.Lock()
        self.export_calls = 0
        self.get_calls = 0
        self.get_offsets = []
        self.import_calls = 0

    async def invoke(self, rpc, **kwargs):
        if isinstance(rpc, _ExportAuthorization):
            self.export_calls += 1
            if self.fail_export:
                raise FloodWait(1720)
            return _Stub(id=7, bytes=b"import-bytes")
        if isinstance(rpc, _ImportAuthorization):
            self.import_calls += 1
            return _Stub(id=7, bytes=b"ok")
        if isinstance(rpc, _GetFile):
            self.get_calls += 1
            self.get_offsets.append(rpc.offset)
            if self.get_calls <= self.serve_chunks:
                return _UploadFile(bytes=b"x" * MIB)
            return _UploadFile(bytes=b"")
        raise AssertionError(f"unexpected rpc {type(rpc).__name__}")


def test_media_session_exports_once_and_caches(tc):
    FakeMediaSession.instances.clear()
    client = MediaClient(primary_dc=2, file_dc=5)

    async def main():
        s1 = await tc["_media_session_for"](client, 5)
        s2 = await tc["_media_session_for"](client, 5)
        return s1, s2

    s1, s2 = asyncio_run(main())
    assert len(FakeMediaSession.instances) == 1
    assert s1 is s2
    assert s1.dc_id == 5
    assert client.export_calls == 1  # never re-export on cache hit
    assert len([rpc for rpc, _ in s1.invoked if isinstance(rpc, _ImportAuthorization)]) == 1
    assert s1.started and not s1.stopped


def test_media_session_same_dc_returns_client(tc):
    client = MediaClient(primary_dc=5, file_dc=5)
    s = asyncio_run(tc["_media_session_for"](client, 5))
    assert s is client
    assert client.export_calls == 0


def test_media_session_flood_cleans_cache(tc):
    FakeMediaSession.instances.clear()
    client = MediaClient(primary_dc=2, file_dc=5, fail_export=True)

    async def main():
        try:
            await tc["_media_session_for"](client, 5)
        except FloodWait as e:
            return e.value
        raise AssertionError("expected FloodWait")

    assert asyncio_run(main()) == 1720
    assert 5 not in client.media_sessions  # no zombie session left for failover


def test_stream_file_chunks_document_path(tc):
    client = MediaClient(primary_dc=5, file_dc=5, serve_chunks=16)

    async def main():
        out = []
        async for chunk in tc["_stream_file_chunks"](client, "fid", 768, 16):
            out.append(chunk)
        return out

    chunks = asyncio_run(main())
    assert len(chunks) == 16  # exactly chunk_count full chunks; tail clipped downstream
    assert sum(len(c) for c in chunks) == 16 * MIB
    assert client.get_offsets[:3] == [768 * MIB, 769 * MIB, 770 * MIB]  # run_start offset honored
    assert client.export_calls == 0  # same-DC file needs no export


def test_stream_file_chunks_cross_dc_exports_once(tc):
    FakeMediaSession.instances.clear()
    client = MediaClient(primary_dc=2, file_dc=5, serve_chunks=2)

    async def main():
        out = []
        async for chunk in tc["_stream_file_chunks"](client, "fid", 0, 2):
            out.append(chunk)
        return out

    chunks = asyncio_run(main())
    assert len(chunks) == 2  # exactly chunk_count full chunks
    assert client.export_calls == 1  # exported once for the cross-DC session
    assert len(FakeMediaSession.instances) == 1
    s = FakeMediaSession.instances[0]
    assert len([rpc for rpc, _ in s.invoked if isinstance(rpc, _ImportAuthorization)]) == 1


def test_stream_file_chunks_cross_dc_flood_propagates(tc):
    FakeMediaSession.instances.clear()
    client = MediaClient(primary_dc=2, file_dc=5, serve_chunks=2, fail_export=True)

    async def main():
        try:
            async for _ in tc["_stream_file_chunks"](client, "fid", 0, 2):
                pass
        except FloodWait as e:
            return e.value
        raise AssertionError("expected FloodWait")

    assert asyncio_run(main()) == 1720
    assert client.export_calls == 1  # only the one attempt
    assert 5 not in client.media_sessions