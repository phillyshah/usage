"""Implausibly large prices are flagged as likely misreads ('$650' -> 8650)."""
import pytest

from app.db import db
from app.learning.ingest_reference import load_bundled_masters
from app.pipeline.assemble import assemble_and_persist


@pytest.fixture(autouse=True)
def _seed_masters():
    load_bundled_masters()


def _f(val, conf="high"):
    return {"value": val, "confidence": conf}


def _run(price, grand_total=None):
    batch = db.create_batch()
    ticket = db.create_ticket({
        "batch_id": batch["id"],
        "source_filename": "price-sanity-test.jpg",
        "status": "pending_review",
    })
    labels = [{"gtin": None, "ref": "ZZPRICE-REF", "lot": "ZZLOT99",
               "expiry": None, "mfg": None, "serial": None,
               "raw": "...", "decoded": True}]
    vision = {
        "header": {"entity": _f("Maxx Orthopedics"), "rep": _f(None, "low"),
                   "rep_code": _f(None, "low"), "surgeon": _f(None, "low"),
                   "hospital": _f(None, "low"), "surgery_date": _f(None, "low"),
                   "po_number": _f(None, "low")},
        "lines": [
            {"index": 0, "ref": _f("ZZPRICE-REF"), "lot": _f("ZZLOT99"),
             "qty": _f(None), "unit_price": _f(price), "wasted": _f(False)},
        ],
        "freight": _f(None, "low"),
        "grand_total": _f(grand_total) if grand_total is not None else _f(None, "low"),
    }
    assemble_and_persist(ticket, vision, labels)
    return db.lines_for_ticket(ticket["ticket_id"])[0], ticket["ticket_id"]


def test_price_over_limit_flagged():
    row, tid = _run(8650)
    assert any("Unusually large price" in f for f in row["flags"])
    fes = db.field_extractions_for_ticket(tid)
    price_fe = [fe for fe in fes if fe.get("field_name") == "unit_price"
                and fe.get("line_id")][0]
    assert price_fe["confidence"] != "high"


def test_normal_price_not_flagged():
    row, _ = _run(1900)
    assert not any("Unusually large price" in f for f in row["flags"])
