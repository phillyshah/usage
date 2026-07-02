"""Regression test for the POST /debug/trace endpoint.

Guards against the v2.7.3 bug where process_ticket was missing from the
main.py import, causing every debug trace to 500 with NameError.
"""
import io
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _jpeg_bytes() -> bytes:
    import cv2
    img = np.zeros((400, 600, 3), np.uint8)
    img[50:150, 50:550] = 255
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


def test_debug_trace_reachable_and_not_500():
    """Endpoint must not raise NameError or any import-level error.

    In offline mode with no Anthropic key, ingest_image routes the ticket to
    manual_queue (no patient sticker found on the blank synthetic image), which
    is fine — the important assertion is that the endpoint returns 200 with a
    JSON body, not a 500 Internal Server Error.
    """
    data = _jpeg_bytes()
    r = client.post(
        "/debug/trace",
        files={"file": ("test-ticket.jpg", io.BytesIO(data), "image/jpeg")},
    )
    # Any structured JSON response is acceptable — 500 is the failure mode.
    assert r.status_code == 200
    body = r.json()
    assert "ticket_id" in body
    assert "status" in body


def test_debug_trace_ok_path_returns_steps():
    """When ingest succeeds and pipeline runs, steps list is populated."""
    from app.pipeline.run import process_ticket

    fake_ticket = {
        "id": "t-debug-01",
        "batch_id": "b-debug-01",
        "source_filename": "test.jpg",
        "image_data": _jpeg_bytes(),
        "status": "pending_review",
    }
    fake_ingest = lambda data, filename, batch_id: {
        "ticket_id": "t-debug-01",
        "status": "pending_review",
    }
    fake_get_ticket = lambda tid: fake_ticket
    fake_create_batch = lambda: {"id": "b-debug-01"}
    fake_process = lambda ticket: {"lines": [], "header": {}, "confidence": {}}

    with (
        patch("app.main.ingest_image", fake_ingest),
        patch("app.main.db.get_ticket", fake_get_ticket),
        patch("app.main.db.create_batch", fake_create_batch),
        patch("app.main.process_ticket", fake_process),
    ):
        data = _jpeg_bytes()
        r = client.post(
            "/debug/trace",
            files={"file": ("ticket.jpg", io.BytesIO(data), "image/jpeg")},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert isinstance(body["steps"], list)
    assert body["filename"] == "ticket.jpg"
