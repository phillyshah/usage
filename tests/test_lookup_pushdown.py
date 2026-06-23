"""Reference lookups must push their predicate to the datastore, not download
the table and scan in Python.

This guards the production bug where Supabase's default 1000-row select() cap
silently broke GTIN/part/lot resolution for any row past the first 1000 (every
ticket came back "GTIN not in product master"), while surgeons (559 rows, under
the cap) resolved fine.
"""
from unittest.mock import MagicMock

from app.db import db


def test_lookups_use_predicate_not_full_scan(monkeypatch):
    """Each hot-path lookup must call the backend's find_* (server-side filter),
    and must NOT fall back to select() (the capped full-table read)."""
    fake = MagicMock()
    fake.find_one.return_value = {"gtin_14": "X", "part_number": "X", "lot": "X",
                                  "sku": "S", "part_no": "X", "rep_name": "R",
                                  "part_no_": "X"}
    fake.find_one_ci.return_value = None
    fake.find_all.return_value = [{"surgeon_distcode": "K", "status": "Active"}]
    fake.select.side_effect = AssertionError(
        "lookup used a full-table select() — it must push the predicate to the DB")
    monkeypatch.setattr(db, "backend", fake)

    db.sku_for_gtin("00810008120088")
    fake.find_one.assert_called_with("reference_gtin", "gtin_14", "00810008120088")

    db.part_info_for_ref("MO-HDAI-36/40-")
    fake.find_one.assert_called_with("reference_part_info", "part_number", "MO-HDAI-36/40-")

    db.lot_lookup("U37142706")
    fake.find_one.assert_called_with("reference_lots", "lot", "U37142706")

    db.surgeon_for_key("MONTIJOMC-001")
    fake.find_all.assert_called_with("reference_surgeons", "surgeon_distcode", "MONTIJOMC-001")

    # None of these touched the capped full-table read.
    fake.select.assert_not_called()


def test_operational_reads_use_predicate_not_full_scan(monkeypatch):
    """field_extractions/line_items/tickets reads must push their filter to the
    DB. field_extractions is the highest-volume table; a capped full-table read
    silently drops recent tickets' confidence + raw snapshots, blanking the
    whole deliverable even though the data was extracted and stored."""
    fake = MagicMock()
    fake.find_all.return_value = []
    fake.find_one.return_value = None
    fake.select.side_effect = AssertionError(
        "operational read used a capped full-table select()")
    monkeypatch.setattr(db, "backend", fake)

    db.field_extractions_for_ticket("t1")
    fake.find_all.assert_called_with("field_extractions", "ticket_id", "t1")
    db.lines_for_ticket("t1")
    fake.find_all.assert_called_with("line_items", "ticket_id", "t1")
    db.tickets_for_batch("b1")
    fake.find_all.assert_called_with("tickets", "batch_id", "b1")
    db.pending_tickets()
    fake.find_all.assert_called_with("tickets", "status", "pending_review")
    db.get_ticket("t1")
    fake.find_one.assert_called_with("tickets", "ticket_id", "t1")
    db.get_batch("b1")
    fake.find_one.assert_called_with("batches", "id", "b1")

    fake.select.assert_not_called()


def test_field_extractions_found_beyond_1000_rows(monkeypatch, tmp_path):
    """A ticket whose field_extractions sit past row 1000 are still returned in
    full (the cap that blanked the deliverable)."""
    import app.db as dbmod

    backend = dbmod._LocalBackend(str(tmp_path))
    # 1200 unrelated rows, then the ticket we care about at the very end.
    filler = [{"ticket_id": f"old-{i}", "field_name": "ref", "orig_value": "x"}
              for i in range(1200)]
    mine = [{"ticket_id": "T", "field_name": fn, "orig_value": "v"}
            for fn in ("ref", "raw_blob", "lot")]
    backend.replace_all("field_extractions", filler + mine, key_col="ticket_id")
    monkeypatch.setattr(db, "backend", backend)

    got = db.field_extractions_for_ticket("T")
    assert {r["field_name"] for r in got} == {"ref", "raw_blob", "lot"}


def test_gtin_resolves_for_row_beyond_1000(monkeypatch, tmp_path):
    """End-to-end: a GTIN whose master row sits well past index 1000 still
    resolves. (The local store never caps, but this locks in the contract that
    resolution does not depend on the row's position.)"""
    import app.db as dbmod

    backend = dbmod._LocalBackend(str(tmp_path))
    rows = [{"gtin_14": f"{i:014d}", "sku": f"SKU-{i}", "status": "In Use",
             "ingested_at": "2026-06-23T00:00:00+00:00"} for i in range(2500)]
    backend.replace_all("reference_gtin", rows, key_col="gtin_14")
    monkeypatch.setattr(db, "backend", backend)

    hit = db.sku_for_gtin("00000000002001")  # row 2001, far past the 1000 cap
    assert hit and hit["sku"] == "SKU-2001"
    assert db.sku_for_gtin("00000000000042")["sku"] == "SKU-42"
    assert db.sku_for_gtin("99999999999999") is None
