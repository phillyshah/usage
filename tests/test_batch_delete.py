"""DELETE /batches/{id} permanently purges the batch + its tickets, line items,
field snapshots (and stored images/sheet). 404 for an unknown id."""
from fastapi.testclient import TestClient

from app.db import db
from app.learning.ingest_reference import load_bundled_masters
from app.pipeline.assemble import assemble_and_persist
from app.main import app

client = TestClient(app)


def _f(v, c="high"):
    return {"value": v, "confidence": c}


def _labels():
    return [
        {"gtin": "00810008120088", "lot": "S41122707", "expiry": "2028-10-31",
         "mfg": "2023-11-01", "ref": "MO-MSFC-56/MH", "serial": None,
         "raw": "x", "decoded": True},
        {"gtin": "00810008121849", "lot": "U37142706", "expiry": "2030-11-30",
         "mfg": "2025-12-01", "ref": "MO-SWCC-65/30", "serial": None,
         "raw": "x", "decoded": True},
    ]


def _build_batch():
    load_bundled_masters()
    batch = db.create_batch()
    ticket = db.create_ticket({
        "batch_id": batch["id"], "entity": "Maxx Health",
        "source_filename": "MH.jpg", "status": "pending_review",
    })
    vision = {
        "header": {},
        "lines": [{"unit_price": _f(900)}, {"unit_price": _f(1900)}],
        "freight": _f(0), "grand_total": _f(2800),
    }
    assemble_and_persist(ticket, vision, _labels())
    return batch, ticket


def test_delete_batch_purges_everything():
    batch, ticket = _build_batch()
    batch_id = batch["id"]
    ticket_id = ticket["ticket_id"]

    # Sanity: the batch really has data before we delete it.
    assert db.lines_for_ticket(ticket_id)
    assert db.field_extractions_for_ticket(ticket_id)

    r = client.delete(f"/batches/{batch_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["deleted_batch"] == batch_id
    assert body["tickets_deleted"] >= 1

    # Everything tied to the batch is gone.
    assert db.get_batch(batch_id) is None
    assert db.tickets_for_batch(batch_id) == []
    assert db.lines_for_ticket(ticket_id) == []
    assert db.field_extractions_for_ticket(ticket_id) == []


def test_delete_unknown_batch_404():
    r = client.delete("/batches/does-not-exist")
    assert r.status_code == 404
