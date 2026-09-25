"""Unit tests for apex_stream.ranges — RFC 7233 parsing and chunk math."""

import pytest

from apex_stream.ranges import (
    CHUNK_SIZE_DEFAULT,
    RangeInvalid,
    RangeNotSatisfiable,
    chunk_layout,
    clip_last_chunk,
    parse_range,
    trim_first_chunk,
)

SIZE = 10_000_000  # 10 MiB


class TestParseRange:
    def test_no_header(self):
        assert parse_range(None, SIZE) is None
        assert parse_range("", SIZE) is None
        assert parse_range("   ", SIZE) is None

    def test_open_ended(self):
        rq = parse_range("bytes=0-", SIZE)
        assert rq.start == 0 and rq.end == SIZE - 1 and rq.length == SIZE

    def test_partial(self):
        rq = parse_range("bytes=1048576-2097151", SIZE)
        assert rq.start == 1048576
        assert rq.end == 2097151
        assert rq.length == 1048576
        assert rq.suffix is False

    def test_upper_beyond_size_is_clamped(self):
        rq = parse_range("bytes=9999990-99999999", SIZE)
        assert rq.end == SIZE - 1
        assert rq.length == SIZE - 9999990

    def test_suffix_range(self):
        rq = parse_range("bytes=-1048576", SIZE)
        assert rq.suffix is True
        assert rq.start == SIZE - 1048576
        assert rq.end == SIZE - 1
        assert rq.length == 1048576

    def test_suffix_larger_than_file(self):
        rq = parse_range("bytes=-99999999", SIZE)
        assert rq.start == 0
        assert rq.length == SIZE

    def test_point_range(self):
        rq = parse_range("bytes=5000-5000", SIZE)
        assert rq.start == rq.end == 5000
        assert rq.length == 1

    def test_invalid_syntax(self):
        with pytest.raises(RangeInvalid):
            parse_range("bytes=abc", SIZE)
        with pytest.raises(RangeInvalid):
            parse_range("chars=0-10", SIZE)
        with pytest.raises(RangeInvalid):
            parse_range("bytes=-", SIZE)
        with pytest.raises(RangeInvalid):
            parse_range("bytes=0-10,20-30", SIZE)  # multi-range unsupported

    def test_unsatisfiable(self):
        with pytest.raises(RangeNotSatisfiable):
            parse_range("bytes=99999999-", SIZE)
        with pytest.raises(RangeNotSatisfiable):
            parse_range("bytes=5-2", SIZE)
        with pytest.raises(RangeNotSatisfiable):
            parse_range("bytes=0-0", 0)

    def test_zero_length_file_never_satisfiable(self):
        with pytest.raises(RangeNotSatisfiable):
            parse_range("bytes=0-", 0)


class TestChunkLayout:
    def test_full_file_alignment(self):
        layout = chunk_layout(0, SIZE - 1, SIZE)
        assert layout.start_chunk == 0
        assert layout.end_chunk == SIZE // CHUNK_SIZE_DEFAULT  # last chunk index
        assert layout.skip_leading == 0
        assert layout.total_needed == SIZE

    def test_offset_within_chunk(self):
        off = 1000
        layout = chunk_layout(off, off + 999, SIZE)
        assert layout.start_chunk == 0
        assert layout.skip_leading == off
        assert layout.total_needed == 1000

    def test_second_chunk_boundary(self):
        layout = chunk_layout(CHUNK_SIZE_DEFAULT, CHUNK_SIZE_DEFAULT + 999, SIZE)
        assert layout.start_chunk == 1
        assert layout.skip_leading == 0

    def test_spanning_multiple_chunks(self):
        layout = chunk_layout(500, 2 * CHUNK_SIZE_DEFAULT + 2500, SIZE)
        assert layout.start_chunk == 0
        assert layout.end_chunk == 2
        assert layout.skip_leading == 500
        assert layout.chunk_count == 3

    def test_end_clamped(self):
        layout = chunk_layout(0, 1_000_000_000, SIZE)
        # parse_range clamps the Request, but chunk_layout also clamps robustly
        assert layout.end_chunk == SIZE // CHUNK_SIZE_DEFAULT
        assert layout.chunk_count == (SIZE // CHUNK_SIZE_DEFAULT) + 1

    def test_empty_window_rejected(self):
        with pytest.raises(RangeNotSatisfiable):
            chunk_layout(100, 99, SIZE)


class TestChunkTrimming:
    def test_trim_first_chunk(self):
        chunk = b"x" * 100
        assert trim_first_chunk(chunk, 0) == chunk
        assert trim_first_chunk(chunk, 40) == b"x" * 60
        assert trim_first_chunk(chunk, 100) == b""
        assert trim_first_chunk(chunk, 1000) == b""
        assert trim_first_chunk(b"", 5) == b""

    def test_clip_last_chunk(self):
        chunk = b"y" * 100
        assert clip_last_chunk(chunk, 1000, 0) == chunk
        assert clip_last_chunk(chunk, 1000, 950) == b"y" * 50
        assert clip_last_chunk(chunk, 1000, 1000) == b""
        assert clip_last_chunk(chunk, 1000, 1100) == b""