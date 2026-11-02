"""What the process weighs, logged at each heavy stage.

Written after two wrong diagnoses in two days. On 2 Nov the kernel killed
uvicorn five times at ~5.3 GB, and every answer about WHY was inference from
reading code — the logs said nothing, because an OOM kill leaves no traceback
and the process simply stops. Twice a plausible cause was found, fixed, and
shipped, and twice the failure came back.

The gap was never the fixing. It was that nobody could see memory move.

So this is deliberately **always on** rather than behind a flag. The failure
arrives without warning and kills the process outright, so there is no moment
at which somebody could turn tracing on first — by the time you want the data,
the run that would have produced it is already over. One read of
/proc/self/statm costs microseconds, which is nothing against the price of
another day spent guessing.

The last `mem ` line before a restart names the stage that was running when the
kernel fired, and the deltas say what grew to get there.
"""
from __future__ import annotations

import logging
import os
import resource
import threading

log = logging.getLogger("usage.mem")

_PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
_last = threading.local()


def rss_mb() -> float:
    """Resident set size right now, in MB. 0.0 where /proc is unavailable."""
    try:
        with open("/proc/self/statm", "rb") as fh:
            return int(fh.read().split()[1]) * _PAGE / (1024 * 1024)
    except Exception:
        return 0.0


def peak_mb() -> float:
    """The highest RSS this process has reached, in MB."""
    try:
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except Exception:
        return 0.0


def mark(stage: str, **extra) -> float:
    """Log the current RSS and the change since this thread's last mark.

    The delta is per thread because the batch runs three tickets at once and a
    single global previous-value would attribute one thread's growth to
    another.
    """
    now = rss_mb()
    prev = getattr(_last, "value", None)
    _last.value = now
    delta = "" if prev is None else f" delta={now - prev:+.0f}MB"
    detail = "".join(f" {k}={v}" for k, v in extra.items())
    log.info("mem stage=%s rss=%.0fMB%s peak=%.0fMB%s",
             stage, now, delta, peak_mb(), detail)
    return now


def reset() -> None:
    """Forget this thread's previous mark, so the next delta starts fresh."""
    _last.value = None
