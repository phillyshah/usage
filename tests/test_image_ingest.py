"""Regression tests for the POST /images hardening (v1.x prod fix).

Background: a single bad photo used to 500 the whole batch — in prod the trigger
was a Supabase schema error (PGRST204: the `source_filename` column was missing
because db/08 was never applied). The fix isolates each file (a failure becomes a
per-file "error" status instead of a 500) and keeps the PHI gate airtight: if the
redacted image can't be encoded we store NOTHING (never the raw upload), and if
storage fails we flip the ticket to manual_queue and re-raise.

These tests lock that behavior in. They run hermetically: conftest forces
OFFLINE_MODE so there is no network and schema_check() always passes.
"""
import numpy as np
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

import app.main as main
import app.pipeline.run as run
from app.db import db
from app.main import app
from app.pipeline.run import ingest_image

client = TestClient(app)


def _jpeg_bytes() -> bytes:
    """A tiny real JPEG the pipeline can decode (content is irrelevant — the
    redaction step is patched in tests that need it to reach the encode gate)."""
    import cv2

    img = np.zeros((400, 600, 3), np.uint8)
    img[50:150, 50:550] = 255  # some white content so it isn't a degenerate frame
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


# ---------------------------------------------------------------------------
# 1. Per-file isolation: one bad file must not 500 the whole upload.
# ---------------------------------------------------------------------------
def test_one_bad_file_does_not_fail_the_batch():
    def fake_ingest(data, filename, batch_id):
        if "bad" in filename:
            # Mimic the prod Supabase schema error that triggered the original 500.
            raise Exception(
                "{'code': 'PGRST204', 'message': \"Could not find the "
                "'source_filename' column of 'tickets' in the schema cache\"}"
            )
        return {"ticket_id": "good-ticket", "status": "pending_review"}

    with patch.object(main, "ingest_image", side_effect=fake_ingest):
        r = client.post(
            "/images",
            files=[
                ("files", ("good.jpg", _jpeg_bytes(), "image/jpeg")),
                ("files", ("bad.jpg", _jpeg_bytes(), "image/jpeg")),
            ],
        )

    # The whole request must succeed (202), not 500 on the one bad file.
    assert r.status_code == 202
    body = r.json()
    assert body["batch_id"]
    tickets = body["tickets"]
    assert len(tickets) == 2

    good = next(t for t in tickets if t["filename"] == "good.jpg")
    bad = next(t for t in tickets if t["filename"] == "bad.jpg")

    # The good file is unaffected by its noisy neighbor.
    assert good["status"] == "pending_review"
    assert good["ticket_id"] == "good-ticket"

    # The bad file is reported per-file with an actionable message (not a stack
    # trace) that tells the operator to run the pending migrations.
    assert bad["status"] == "error"
    assert bad["ticket_id"] is None
    assert bad["error"]
    assert "migration" in bad["error"].lower()


# ---------------------------------------------------------------------------
# 2. All-good path still works.
# ---------------------------------------------------------------------------
def test_all_good_files_ingest():
    def fake_ingest(data, filename, batch_id):
        return {"ticket_id": f"tid-{filename}", "status": "pending_review"}

    with patch.object(main, "ingest_image", side_effect=fake_ingest):
        r = client.post(
            "/images",
            files=[
                ("files", ("a.jpg", _jpeg_bytes(), "image/jpeg")),
                ("files", ("b.jpg", _jpeg_bytes(), "image/jpeg")),
            ],
        )

    assert r.status_code == 202
    body = r.json()
    assert body["batch_id"]
    tickets = body["tickets"]
    assert len(tickets) == 2
    assert all(t["status"] == "pending_review" for t in tickets)


# ---------------------------------------------------------------------------
# 3. PHI gate: encode failure -> manual_queue, store NOTHING (never raw bytes).
# ---------------------------------------------------------------------------
def test_encode_failure_routes_to_manual_queue_and_stores_nothing():
    batch = db.create_batch()

    fake_storage = Mock()
    # Force the path to the encode gate deterministically: the region "locates"
    # (located=True) so we reach the encode step, then encoding yields empty bytes.
    redacted = np.zeros((400, 600, 3), np.uint8)
    with patch.object(run, "redact_patient_region", return_value=(redacted, True)), \
         patch.object(run.preprocess, "encode_image", return_value=b""), \
         patch.object(run, "put_object", fake_storage):
        res = ingest_image(_jpeg_bytes(), "MO-test.jpg", batch["id"])

    # Encode failed -> we cannot prove the bytes are masked -> manual queue.
    assert res["status"] == "manual_queue"
    ticket = db.get_ticket(res["ticket_id"])
    assert not ticket.get("source_image_path")  # nothing stored

    # Crucially: storage was never touched, so no raw (or unredacted) bytes leaked.
    fake_storage.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Storage failure -> ticket flipped to manual_queue and the error propagates.
# ---------------------------------------------------------------------------
def test_storage_failure_flips_ticket_to_manual_queue_and_raises():
    batch = db.create_batch()

    redacted = np.zeros((400, 600, 3), np.uint8)
    boom = Exception("storage down")
    with patch.object(run, "redact_patient_region", return_value=(redacted, True)), \
         patch.object(run.preprocess, "encode_image", return_value=b"jpegbytes"), \
         patch.object(run, "put_object", side_effect=boom):
        try:
            ingest_image(_jpeg_bytes(), "MO-store-fail.jpg", batch["id"])
        except Exception as exc:
            raised = exc
        else:
            raised = None

    # The error must propagate so the caller can report it per-file.
    assert raised is boom

    # The ticket that was created must not be left pointing at nothing: it is
    # flipped to manual_queue with no stored image path.
    tickets = db.tickets_for_batch(batch["id"])
    assert len(tickets) == 1
    ticket = tickets[0]
    assert ticket["status"] == "manual_queue"
    assert not ticket.get("source_image_path")


# ---------------------------------------------------------------------------
# 5. schema_check is clean offline; /diag reports offline with no probes.
# ---------------------------------------------------------------------------
def test_schema_check_clean_offline_and_diag():
    # The local JSON store has every column implicitly -> no schema problems.
    assert db.schema_check() == []

    r = client.get("/diag")
    assert r.status_code == 200
    body = r.json()
    assert body["datastore"] == "offline"
    # Offline never probes Supabase, so these supabase-only fields are absent.
    assert "schema_problems" not in body
    assert "schema_ok" not in body


# ---------------------------------------------------------------------------
# 6. _explain_db_error turns raw datastore errors into actionable messages.
# ---------------------------------------------------------------------------
def test_explain_db_error_messages():
    # PGRST204 (missing column/table) -> point at the pending migrations in db/.
    pgrst = Exception(
        "{'code': 'PGRST204', 'message': \"Could not find the 'source_filename' "
        "column of 'tickets' in the schema cache\"}"
    )
    msg = main._explain_db_error(pgrst)
    assert "migration" in msg.lower()
    assert "db/" in msg

    # 42501 (row-level security) -> explain the key/RLS problem.
    rls = Exception("42501: new row violates row-level security policy")
    msg = main._explain_db_error(rls)
    assert "row-level security" in msg.lower()

    # Anything else -> generic fallthrough that still surfaces the cause.
    generic = Exception("something unexpected exploded")
    msg = main._explain_db_error(generic)
    assert "could not process the request" in msg.lower()
    assert "something unexpected exploded" in msg
