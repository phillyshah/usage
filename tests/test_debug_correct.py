"""POST /debug/trace/{ticket_id}/correct — inline training from the Debug
Console. A trace already persists real ticket/line_items/field_extractions
rows; this endpoint lets the operator confirm or correct them in place and
feeds the same harvest/diff learning pipeline the .xlsx upload path uses.
"""
import io
from unittest.mock import patch

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.db import db
from app.learning.ingest_reference import load_bundled_masters
from app.main import app
from app.pipeline.assemble import assemble_and_persist

client = TestClient(app)


@pytest.fixture(autouse=True)
def _seed_masters():
    load_bundled_masters()


def _f(val, conf="high"):
    return {"value": val, "confidence": conf}


def _make_ticket_with_one_line(ref="ZZDBG-REF-1", lot="ZZDBGLOT01",
                               price=650, hospital="Debug Hospital",
                               surgeon="Debugson", rep_code="ZZ-DBG-001",
                               description="Debug Widget", gtin=None):
    """Persists a real ticket + line via the real assemble pipeline, so
    field_extractions/line_items exist for the correct endpoint to read/diff.

    Seeds reference_part_info BEFORE assembling so resolve_part naturally
    resolves the description at assembly time — keeping field_extractions
    (the correction-diff snapshot) and line_items consistent, the same way a
    real ticket would be.

    gtin defaults to one derived from `ref` (not a shared constant): tests run
    in the same process, and since v2.9.0 resolve_part falls back to the
    learned GTIN->REF crosswalk, a GTIN reused across tests with different
    refs would resolve to whichever ref a PRIOR test taught it — a real
    feature, but it means tests must not share a GTIN across different refs.
    """
    gtin = gtin or f"00999{abs(hash(ref)) % 10**9:09d}"
    db.backend.upsert("reference_part_info", ["part_number"], {
        "part_number": ref, "description": description,
        "part_type": "Test Part", "category": "Test",
    })
    batch = db.create_batch()
    ticket = db.create_ticket({
        "batch_id": batch["id"],
        "source_filename": "debug-correct-test.jpg",
        "status": "pending_review",
    })
    labels = [{"gtin": gtin, "ref": ref, "lot": lot,
               "expiry": None, "mfg": None, "serial": None,
               "raw": "...", "decoded": True}]
    vision = {
        "header": {
            "entity": _f("Maxx Orthopedics"), "rep": _f(None, "low"),
            "rep_code": _f(rep_code), "surgeon": _f(surgeon),
            "hospital": _f(hospital), "surgery_date": _f(None, "low"),
            "po_number": _f(None, "low"),
        },
        "lines": [
            {"index": 0, "ref": _f(ref), "lot": _f(lot), "qty": _f(None),
             "unit_price": _f(price), "wasted": _f(False)},
        ],
        "freight": _f(None, "low"),
        "grand_total": _f(None, "low"),
    }
    assemble_and_persist(ticket, vision, labels)
    line = db.lines_for_ticket(ticket["ticket_id"])[0]
    return ticket["ticket_id"], line["line_id"]


def test_debug_correct_unknown_ticket_404():
    r = client.post("/debug/trace/no-such-ticket/correct", json={"confirm_all": True})
    assert r.status_code == 404


def test_debug_correct_confirm_all_marks_verified():
    ticket_id, _ = _make_ticket_with_one_line()
    r = client.post(f"/debug/trace/{ticket_id}/correct", json={"confirm_all": True})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "verified"
    assert db.get_ticket(ticket_id)["status"] == "verified"
    assert body["audited_fields"] == 0   # nothing changed vs. its own snapshot
    assert body["learned"]["price"] >= 1


def test_debug_correct_partial_edit_audits_only_changed_field():
    ticket_id, line_id = _make_ticket_with_one_line()
    r = client.post(f"/debug/trace/{ticket_id}/correct", json={
        "lines": {line_id: {"unit_price": 725}},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["audited_fields"] == 1
    audit_rows = [row for row in db.backend.select("corrections_audit")
                  if row["ticket_id"] == ticket_id]
    assert len(audit_rows) == 1
    assert audit_rows[0]["line_id"] == line_id
    assert audit_rows[0]["field_name"] == "unit_price"
    assert audit_rows[0]["corrected_value"] == "725"


def test_debug_correct_untouched_fields_use_current_stored_value():
    ticket_id, line_id = _make_ticket_with_one_line(description="Original Description")
    client.post(f"/debug/trace/{ticket_id}/correct", json={
        "lines": {line_id: {"unit_price": 999}},
    })
    rows = db.learning_part_descs()
    matches = [r for r in rows if r["part_no"] == "ZZDBG-REF-1"]
    assert matches, "description should still be harvested from the untouched stored value"
    assert matches[0]["description"] == "Original Description"


def test_debug_correct_updates_learning_tables():
    gtin = "00999888877002"
    ticket_id, line_id = _make_ticket_with_one_line(
        ref="ZZDBG-REF-2", hospital="Learning Test Hospital",
        surgeon="Learnson", rep_code="ZZ-LRN-001", description="Learned Widget",
        gtin=gtin)
    r = client.post(f"/debug/trace/{ticket_id}/correct", json={"confirm_all": True})
    assert r.status_code == 200

    assert any(row["part_no"] == "ZZDBG-REF-2" for row in db.learning_prices())
    assert any(row["part_no"] == "ZZDBG-REF-2" for row in db.learning_part_descs())
    assert any(row["gtin"] == gtin for row in db.learning_gtin_xrefs())
    from app.learning.ingest_reference import surgeon_key
    key = surgeon_key("Learnson", "ZZ-LRN-001")
    learned_surg = db.learned_surgeon_for_key(key)
    assert learned_surg is not None
    assert learned_surg["hospital"] == "Learning Test Hospital"


def test_debug_correct_response_shape():
    ticket_id, _ = _make_ticket_with_one_line()
    r = client.post(f"/debug/trace/{ticket_id}/correct", json={"confirm_all": True})
    body = r.json()
    assert set(body.keys()) >= {"ticket_id", "status", "learned", "audited_fields"}
    assert set(body["learned"].keys()) == {"part_desc", "rep", "price", "gtin_xref", "surgeon_map"}
    assert isinstance(body["audited_fields"], int)


def test_debug_correct_idempotent_confirm_all():
    ticket_id, _ = _make_ticket_with_one_line()
    r1 = client.post(f"/debug/trace/{ticket_id}/correct", json={"confirm_all": True})
    count_after_1 = len([row for row in db.backend.select("corrections_audit")
                         if row["ticket_id"] == ticket_id])
    r2 = client.post(f"/debug/trace/{ticket_id}/correct", json={"confirm_all": True})
    count_after_2 = len([row for row in db.backend.select("corrections_audit")
                         if row["ticket_id"] == ticket_id])
    assert r1.status_code == 200 and r2.status_code == 200
    assert count_after_1 == count_after_2 == 0
    assert db.get_ticket(ticket_id)["status"] == "verified"


def _jpeg_bytes() -> bytes:
    import cv2
    img = np.zeros((400, 600, 3), np.uint8)
    img[50:150, 50:550] = 255
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


def test_debug_trace_response_includes_header_lines_confidence():
    """Extends test_debug_trace.py's contract: a real trace attaches the
    authoritative header/lines/confidence the review form renders from."""
    fake_ticket = {"ticket_id": "t-debug-fields", "status": "pending_review"}
    fake_ingest = lambda data, filename, batch_id: {
        "ticket_id": "t-debug-fields", "status": "pending_review",
    }
    fake_create_batch = lambda: {"id": "b-debug-fields"}
    fake_process = lambda ticket: {"ticket_id": "t-debug-fields", "line_count": 0, "flags": []}

    with (
        patch("app.main.ingest_image", fake_ingest),
        patch("app.main.db.get_ticket", lambda tid: fake_ticket),
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
    result = body["result"]
    assert "header" in result and isinstance(result["header"], dict)
    assert "lines" in result and isinstance(result["lines"], list)
    assert "confidence" in result
    assert set(result["confidence"].keys()) == {"header", "lines"}
