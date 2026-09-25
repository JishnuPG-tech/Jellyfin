"""Streaming error taxonomy: source vs transport failures.

The pool must only cool down a client on *transport* failures (MTProto / network
health). *Source* failures — the chat/message/file itself is missing, malformed,
shorter than recorded, or holds an expired Telegram file reference — mean the
source needs a refresh/failover, never that the client is sick.
"""


class StreamError(Exception):
    """Base class for apex_stream streaming errors."""


class SourceError(StreamError):
    """The source (chat/message/file) itself is broken or unreachable.

    A perfectly healthy pool client can hit this (e.g. the stored Telegram
    media reference expired and could not be refreshed through this client).
    It must never trip client cooldown, but retrying over another client is
    allowed — another pool member may resolve the source successfully.
    """


class SourceReferenceExpired(SourceError):
    """The cached Telegram media file reference expired.

    Raised only after an automatic per-client refresh re-fetch also failed, so
    the driver can fail the run over to the next client (which retries its own
    refresh through its own get_messages()).
    """