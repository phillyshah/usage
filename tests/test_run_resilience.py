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
from pathlib import Path

import cv2
import numpy as np

from app.pipeline import transient


def _tiny_jpeg() -> bytes:
    ok, buf = cv2.imencode(".jpg", np.full((400, 600, 3), 255, np.uint8))
    assert ok
    return buf.tobytes()
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
    monkeypatch.setattr(transient.time, "sleep", lambda *_: None)  # no real backoff
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
    monkeypatch.setattr(transient.time, "sleep", lambda *_: None)
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


# ---------------------------------------------------------------------------
# Upload had no retry at all, and that cost four photos off a real upload.
#
# Several uploads share one HTTP/2 storage client. Under contention it drops
# connections — "Server disconnected" — and the per-file isolation that was
# meant to stop one bad file sinking the batch was instead discarding perfectly
# good photos. Rising volume makes that contention more likely, not less.
# ---------------------------------------------------------------------------
def test_server_disconnected_is_classified_as_worth_retrying():
    """The exact error four photos died on."""
    class RemoteProtocolError(Exception):
        pass

    assert transient.is_transient(
        RemoteProtocolError("Server disconnected without sending a response."))


def test_retry_gives_up_on_a_failure_that_will_not_get_better(monkeypatch):
    monkeypatch.setattr(transient.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def always_bad():
        calls["n"] += 1
        raise ValueError("this file is corrupt")

    with pytest.raises(ValueError):
        transient.retry(always_bad)
    assert calls["n"] == 1, "a corrupt file is not worth trying four times"


def test_retry_returns_the_value_once_the_blip_passes(monkeypatch):
    monkeypatch.setattr(transient.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    class ConnectError(Exception):
        pass

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectError("Server disconnected")
        return {"ticket_id": "t1", "status": "pending_review"}

    assert transient.retry(flaky)["ticket_id"] == "t1"
    assert calls["n"] == 3


def test_an_upload_survives_a_dropped_connection(monkeypatch):
    """End to end through the route: the photo lands rather than being reported
    to the user as one that 'couldn't be uploaded'."""
    from fastapi.testclient import TestClient

    import app.main as main

    monkeypatch.setattr(transient.time, "sleep", lambda *_: None)
    calls = {"n": 0}
    real = main.ingest_image

    def flaky_ingest(data, filename, batch_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("Server disconnected without sending a response.")
        return real(data, filename, batch_id)

    monkeypatch.setattr(main, "ingest_image", flaky_ingest)

    img = (Path(__file__).parent / "fixtures" / "one_pixel.jpg")
    data = img.read_bytes() if img.exists() else _tiny_jpeg()
    resp = TestClient(main.app).post(
        "/images", files={"files": ("MO-retry.jpg", data, "image/jpeg")})

    assert resp.status_code == 202
    ticket = resp.json()["tickets"][0]
    assert ticket["status"] != "error", f"upload was dropped: {ticket.get('error')}"
    assert calls["n"] == 2, "it should have been retried exactly once"
