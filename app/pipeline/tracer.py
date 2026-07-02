"""Per-request trace collector for the debug console.

A ContextVar holds the active trace list for the current request context.
Pipeline functions call record() to deposit steps; the collector is a no-op
when no trace is active, so normal production requests are completely unaffected.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any

_active: ContextVar[list | None] = ContextVar("debug_trace", default=None)


def start() -> list:
    """Activate tracing for this context; returns the live steps list."""
    steps: list = []
    _active.set(steps)
    return steps


def stop() -> None:
    """Deactivate tracing (idempotent)."""
    _active.set(None)


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
        steps.append({
            "stage": stage,
            "label": label,
            "status": status,
            "summary": summary,
            "detail": detail or {},
        })
