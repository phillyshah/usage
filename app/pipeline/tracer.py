"""Per-request trace collector for the debug console.

A ContextVar holds the active trace list for the current request context.
Pipeline functions call record() to deposit steps; the collector is a no-op
when no trace is active, so normal production requests are completely unaffected.
"""
from __future__ import annotations

import time
from contextvars import ContextVar
from typing import Any

_active: ContextVar[list | None] = ContextVar("debug_trace", default=None)
# When the previous step finished, so each step can report how long it took.
# "Is two minutes normal?" was not answerable from a trace that recorded what
# happened but never how long any of it took.
_since: ContextVar[float | None] = ContextVar("debug_trace_since", default=None)


def start() -> list:
    """Activate tracing for this context; returns the live steps list."""
    steps: list = []
    _active.set(steps)
    _since.set(time.monotonic())
    return steps


def stop() -> None:
    """Deactivate tracing (idempotent)."""
    _active.set(None)
    _since.set(None)


def record(
    stage: str,
    label: str,
    status: str,
    summary: str,
    detail: Any = None,
) -> None:
    """Append one step to the active trace (no-op when no trace is active).

    status: "ok" | "warn" | "miss" | "fail" | "skip"
    """
    steps = _active.get()
    if steps is not None:
        now = time.monotonic()
        started = _since.get()
        _since.set(now)
        steps.append({
            "stage": stage,
            "label": label,
            "status": status,
            "summary": summary,
            "ms": None if started is None else int((now - started) * 1000),
            "detail": detail or {},
        })
