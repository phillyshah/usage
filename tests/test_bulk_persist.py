"""Persistence must be bulk: a ticket's line items + field snapshots are written
in single round-trips, not one INSERT per row (the extraction-speed fix)."""
from app.db import db
from app.learning.ingest_reference import load_bundled_masters
from app.pipeline.assemble import LINE_FIELDS, TICKET_FIELDS, assemble_and_persist


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


def test_persist_uses_bulk_inserts(monkeypatch):
    load_bundled_masters()
    calls = {"insert": [], "insert_many": []}
    real_insert, real_many = db.backend.insert, db.backend.insert_many

    def spy_insert(table, row):
        calls["insert"].append(table)
        return real_insert(table, row)

    def spy_many(table, rows):
        calls["insert_many"].append((table, len(rows)))
        return real_many(table, rows)

    monkeypatch.setattr(db.backend, "insert", spy_insert)
    monkeypatch.setattr(db.backend, "insert_many", spy_many)

    batch = db.create_batch()
    ticket = db.create_ticket({"batch_id": batch["id"], "entity": "Maxx Health",
                               "source_filename": "MH.jpg", "status": "pending_review"})
    vision = {"header": {}, "grand_total": _f(968),
              "lines": [{"unit_price": _f(900)}, {"unit_price": _f(68)}]}
    assemble_and_persist(ticket, vision, _labels())

    # No per-row inserts into the high-volume tables.
    assert "field_extractions" not in calls["insert"]
    assert "line_items" not in calls["insert"]

    # Exactly one bulk insert each, with the expected row counts.
    li = [n for (t, n) in calls["insert_many"] if t == "line_items"]
    fe = [n for (t, n) in calls["insert_many"] if t == "field_extractions"]
    assert li == [2]                                   # 2 line items, one call
    # per line: 1 raw_blob + len(LINE_FIELDS); plus len(TICKET_FIELDS) header rows
    assert fe == [2 * (1 + len(LINE_FIELDS)) + len(TICKET_FIELDS)]
