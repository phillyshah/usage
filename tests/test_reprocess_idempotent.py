"""Re-processing a ticket must be idempotent: re-running Extract replaces the
ticket's line items + field snapshots instead of stacking duplicates."""
from app.db import db
from app.learning.ingest_reference import load_bundled_masters
from app.pipeline.assemble import assemble_and_persist


def _f(v, c="high"):
    return {"value": v, "confidence": c}


def _ticket():
    load_bundled_masters()
    batch = db.create_batch()
    return db.create_ticket({"batch_id": batch["id"], "entity": "Maxx Health",
                             "source_filename": "MH-x.jpg", "status": "pending_review"})


def _labels():
    return [
        {"gtin": "00810008120088", "lot": "S41122707", "mfg": "2023-11-01",
         "expiry": "2028-10-31", "ref": "MO-MSFC-56/MH", "serial": None,
         "raw": "010081...", "decoded": True},
        {"gtin": "00810008121849", "lot": "U37142706", "mfg": "2025-12-01",
         "expiry": "2030-11-30", "ref": "MO-SWCC-65/30", "serial": None,
         "raw": "010081...", "decoded": True},
    ]


def test_reprocess_replaces_not_duplicates():
    ticket = _ticket()
    vision = {"header": {}, "lines": [{"unit_price": _f(900.0)}, {"unit_price": _f(68.0)}]}

    assemble_and_persist(ticket, vision, _labels())
    first = db.lines_for_ticket(ticket["ticket_id"])
    assert len(first) == 2

    # Re-run extraction on the same ticket (what clicking Extract again does).
    assemble_and_persist(ticket, vision, _labels())
    second = db.lines_for_ticket(ticket["ticket_id"])
    assert len(second) == 2, "re-processing must replace, not append"

    # Field extractions for the ticket are likewise replaced, not doubled.
    fes = db.field_extractions_for_ticket(ticket["ticket_id"])
    raw_blobs = [f for f in fes if f.get("field_name") == "raw_blob"]
    assert len(raw_blobs) == 2  # one per line, not four
