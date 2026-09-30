"""Is this exception worth retrying?

Lives on its own because two callers need the same answer and must not drift:
``run._safe_process`` decides whether to re-run a whole ticket, and
``vision.extract_handwritten`` decides whether to re-raise so that retry can
happen at all. Before this was shared, vision swallowed every exception —
including the overload and rate-limit errors this function was written to
catch — so the retry loop could never see them.
"""
from __future__ import annotations

# Matched by type NAME and by message, so we don't hard-depend on h2 / httpx /
# anthropic being importable here.
TRANSIENT_TYPES = {
    "ConnectionTerminated", "RemoteProtocolError", "ConnectError", "ConnectTimeout",
    "ReadError", "ReadTimeout", "WriteError", "PoolTimeout", "ConnectionError",
    "APIConnectionError", "APITimeoutError", "RateLimitError", "InternalServerError",
    "ServerDisconnectedError", "APIStatusError", "OverloadedError",
}

_TRANSIENT_TEXT = (
    "connectionterminated", "goaway", "server disconnected", "connection reset",
    "connection aborted", "overloaded", "timed out", "timeout",
    "502", "503", "504", "529",
)


def is_transient(exc: Exception) -> bool:
    """True for connection-level and server-side failures worth retrying.

    Chiefly the HTTP/2 GOAWAY / ConnectionTerminated the shared Supabase client
    throws when several tickets are processed at once, plus the usual transient
    network and overload errors from the vision API.
    """
    names = {type(exc).__name__}
    for ctx in (exc.__cause__, exc.__context__):
        if ctx is not None:
            names.add(type(ctx).__name__)
    if names & TRANSIENT_TYPES:
        return True
    blob = f"{type(exc).__name__}: {exc}".lower()
    return any(k in blob for k in _TRANSIENT_TEXT)
