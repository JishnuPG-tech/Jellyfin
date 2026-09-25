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

# Stub pyrogram.raw so AST-extracted _warm_channel_peer can resolve
# raw.functions.channels / raw.types without the real package installed.
_pg = types.ModuleType("pyrogram")
_raw = types.ModuleType("pyrogram.raw")
_funcs = types.ModuleType("pyrogram.raw.functions")
_channels = types.ModuleType("pyrogram.raw.functions.channels")
_rtypes = types.ModuleType("pyrogram.raw.types")


class _Stub:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _GetChannels(_Stub):
    pass


class _InputChannel(_Stub):
    pass


_channels.GetChannels = _GetChannels
_rtypes.InputChannel = _InputChannel
_funcs.channels = _channels
_raw.functions = _funcs
_raw.types = _rtypes
_pg.raw = _raw
sys.modules["pyrogram"] = _pg
sys.modules["pyrogram.raw"] = _raw
sys.modules["pyrogram.raw.functions"] = _funcs
sys.modules["pyrogram.raw.functions.channels"] = _channels
sys.modules["pyrogram.raw.types"] = _rtypes

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