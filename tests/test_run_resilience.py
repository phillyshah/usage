"""Batch processing must survive transient connection failures.

A multi-page PDF split into 3 tickets but 2 came back empty with
"Processing error: <ConnectionTerminated …>" — the shared Supabase HTTP/2
client throwing GOAWAY under concurrent ticket processing. run_batch now retries
transient errors per ticket (safe because re-processing is idempotent).
"""
from unittest.mock import patch

import httpx
import pytest

import app.pipeline.run as run
from app.db import db


class _ConnectionTerminated(Exception):
    """Stand-in matching the h2 exception name the classifier keys on."""
    pass


_ConnectionTerminated.__name__ = "ConnectionTerminated"


@pytest.mark.parametrize("exc,transient", [
    (_ConnectionTerminated("error_code 0"), True),
    (httpx.RemoteProtocolError("server disconnected"), True),
    (httpx.ConnectError("conn refused"), True),
    (Exception("529 Overloaded"), True),
    (Exception("read timed out"), True),
    (ValueError("bad ref"), False),
    (KeyError("missing"), False),
])
def test_is_transient_classifier(exc, transient):
    assert run._is_transient(exc) is transient


def test_safe_process_retries_transient_then_succeeds(monkeypatch):
    monkeypatch.setattr(run.time, "sleep", lambda *_: None)  # no real backoff
    calls = {"n": 0}

    def flaky(ticket):
        calls["n"] += 1
        if calls["n"] < 3:                     # fail twice, succeed on the 3rd
            raise _ConnectionTerminated("error_code 0")

    flagged = {}
    monkeypatch.setattr(run, "process_ticket", flaky)
    monkeypatch.setattr(run.db, "update_ticket", lambda tid, patch: flagged.setdefault(tid, patch))

    run._safe_process({"ticket_id": "T1"})
    assert calls["n"] == 3                      # retried until success
    assert "T1" not in flagged                  # never flagged a processing error


def test_safe_process_does_not_retry_non_transient(monkeypatch):
    monkeypatch.setattr(run.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def boom(ticket):
        calls["n"] += 1
        raise ValueError("a real bug")

    flagged = {}
    monkeypatch.setattr(run, "process_ticket", boom)
    monkeypatch.setattr(run.db, "update_ticket", lambda tid, patch: flagged.update({tid: patch}))

    run._safe_process({"ticket_id": "T2"})
    assert calls["n"] == 1                       # non-transient -> fail fast
    assert "Processing error" in flagged["T2"]["flags"][0]
