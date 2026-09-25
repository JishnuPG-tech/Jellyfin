"""RFC 7233 Range parsing and byte-layout math.

Pure, dependency-free logic so it can be unit-tested without a server.
"""

import re
from dataclasses import dataclass

CHUNK_SIZE_DEFAULT = 1 * 1024 * 1024  # 1 MiB logical Telegram chunk

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


class RangeNotSatisfiable(Exception):
    """Raised for syntactically valid but unsatisfiable Range headers (416)."""


class RangeInvalid(Exception):
    """Raised for malformed Range headers that we refuse to guess at (416)."""


@dataclass(frozen=True)
class RangeRequest:
    start: int
    end: int
    length: int
    suffix: bool = False

    @property
    def satisfied(self) -> bool:
        return self.length > 0


def parse_range(header: str | None, file_size: int) -> RangeRequest | None:
    """Parse a single HTTP Range header into a byte window.

    Returns None when there is no Range header (full file).
    Raises RangeNotSatisfiable when the requested range cannot be satisfied
    (out of bounds / zero length) and RangeInvalid on malformed syntax.
    """
    if header is None or header.strip() == "":
        return None

    match = _RANGE_RE.match(header.strip())
    if not match:
        raise RangeInvalid(f"Unsupported or malformed Range header: {header!r}")

    start_s, end_s = match.group(1), match.group(2)

    if start_s == "" and end_s == "":
        raise RangeInvalid(f"Empty range spec: {header!r}")

    if start_s == "":
        # Suffix form: bytes=-N  (last N bytes)
        try:
            n = int(end_s)
        except ValueError:
            raise RangeInvalid(f"Non-numeric suffix length: {header!r}") from None
        if n <= 0:
            raise RangeNotSatisfiable("Suffix length must be positive")
        if file_size == 0:
            raise RangeNotSatisfiable("Cannot produce a range from a zero-length file")
        start = max(file_size - n, 0)
        end = file_size - 1
        return RangeRequest(start=start, end=end, length=end - start + 1, suffix=True)

    try:
        start = int(start_s)
    except ValueError:
        raise RangeInvalid(f"Non-numeric start: {header!r}") from None

    if end_s == "":
        end = file_size - 1
    else:
        try:
            end = int(end_s)
        except ValueError:
            raise RangeInvalid(f"Non-numeric end: {header!r}") from None

    if file_size == 0:
        raise RangeNotSatisfiable("Cannot produce a range from a zero-length file")
    if start >= file_size or start > end:
        raise RangeNotSatisfiable(f"Requested range {start}-{end} vs size {file_size}")
    end = min(end, file_size - 1)
    return RangeRequest(start=start, end=end, length=end - start + 1)


@dataclass(frozen=True)
class ChunkLayout:
    start_chunk: int
    end_chunk: int
    chunk_count: int
    skip_leading: int
    total_needed: int


def chunk_layout(start: int, end: int, file_size: int, chunk_size: int = CHUNK_SIZE_DEFAULT) -> ChunkLayout:
    """Map a byte window onto aligned 1 MiB Telegram chunks.

    start/end are inclusive byte positions of the request window.
    skip_leading is how many bytes of the first Telegram chunk precede `start`
    (and must be trimmed before sending). total_needed is the number of body
    bytes the HTTP response must carry.
    """
    assert chunk_size > 0
    if start < 0 or end < start:
        raise RangeNotSatisfiable("Empty or negative requested window")
    if file_size > 0:
        end = min(end, file_size - 1)
    if start > end:
        raise RangeNotSatisfiable("Empty requested window")

    start_chunk = start // chunk_size
    end_chunk = end // chunk_size
    skip_leading = start % chunk_size
    chunk_count = end_chunk - start_chunk + 1
    total_needed = end - start + 1
    return ChunkLayout(
        start_chunk=start_chunk,
        end_chunk=end_chunk,
        chunk_count=chunk_count,
        skip_leading=skip_leading,
        total_needed=total_needed,
    )


def trim_first_chunk(chunk: bytes, skip_leading: int) -> bytes:
    """Trim the leading `skip_leading` bytes from the first Telegram chunk."""
    if skip_leading <= 0 or not chunk:
        return chunk
    if skip_leading >= len(chunk):
        return b""
    return chunk[skip_leading:]


def clip_last_chunk(chunk: bytes, total_needed: int, emitted: int) -> bytes:
    """Clip a chunk so the response never exceeds the requested window."""
    remaining = total_needed - emitted
    if remaining <= 0:
        return b""
    if len(chunk) > remaining:
        return chunk[:remaining]
    return chunk