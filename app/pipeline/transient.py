"""Is this exception worth retrying?

Lives on its own because two callers need the same answer and must not drift:
``run._safe_process`` decides whether to re-run a whole ticket, and
``vision.extract_handwritten`` decides whether to re-raise so that retry can
happen at all. Before this was shared, vision swallowed every exception —
including the overload and rate-limit errors this function was written to
catch — so the retry loop could never see them.
"""
from __future__ import annotations

import logging
import random
import time

log = logging.getLogger("pipeline.transient")

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


def retry(fn, *args, attempts: int = 4, label: str = "", **kwargs):
    """Call ``fn``, retrying transient failures with exponential backoff.

    Raises the last exception once the attempts are spent, or immediately if
    the failure was never transient — the caller decides what a real failure
    means, this only decides what is worth trying again.

    Shared rather than copied because the two places that need it kept drifting.
    Batch processing had a retry loop from the start; UPLOAD never did, so a
    momentary "Server disconnected" from the storage client dropped four photos
    on the floor while the batch path would have shrugged the same error off.
    Growing volume makes that contention more likely, not less, and the backoff
    is what spreads the load out instead of hammering through it.
    """
    for attempt in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if attempt < attempts - 1 and is_transient(e):
                delay = 0.5 * (2 ** attempt) + random.random() * 0.3
                log.warning("transient failure%s (attempt %d/%d), retrying in %.1fs: %s",
                            f" on {label}" if label else "", attempt + 1, attempts,
                            delay, e)
                time.sleep(delay)
                continue
            raise
