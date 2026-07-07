"""Non-product barcodes (e.g. a patient wristband) must not become lines.

Production bug this guards against: a wristband linear barcode (raw payload
like 'GN2803271987', no GS1 device data) entered the label list, produced a
phantom empty output row, and shifted every barcode↔vision pairing by one.
"""
import pytest

from app.db import db
from app.learning.ingest_reference import load_bundled_masters
from app.pipeline.assemble import assemble_and_persist
from app.pipeline.barcode import drop_junk_labels


@pytest.fixture(autouse=True)
def _seed_masters():
    load_bundled_masters()


def _f(val, conf="high"):
    return {"value": val, "confidence": conf}


WRISTBAND = {"gtin": None, "lot": None, "expiry": None, "mfg": None,
             "serial": None, "ref": None, "raw": "GN2803271987", "decoded": False}
PRODUCT = {"gtin": "00810008121047", "ref": "MO-HDAI-28/00", "lot": "7011879919",
           "expiry": "2030-05-31", "mfg": "2025-06-01", "serial": None,
           "raw": "...", "decoded": True}


def test_wristband_payload_dropped():
    kept = drop_junk_labels([WRISTBAND, PRODUCT])
    assert kept == [PRODUCT]


def test_partial_gs1_label_is_kept():
    # A label that decoded only a lot (no GTIN) is still a real device label.
    partial = dict(WRISTBAND, raw="10S39102702", lot="S39102702")
    assert drop_junk_labels([partial]) == [partial]


def test_wristband_causes_no_phantom_row():
    batch = db.create_batch()
    ticket = db.create_ticket({
        "batch_id": batch["id"],
        "source_filename": "wristband-test.jpg",
        "status": "pending_review",
    })
    labels = drop_junk_labels([WRISTBAND, PRODUCT])
    vision = {
        "header": {"entity": _f("Maxx Health, Inc."), "rep": _f(None, "low"),
                   "rep_code": _f(None, "low"), "surgeon": _f(None, "low"),
                   "hospital": _f(None, "low"), "surgery_date": _f(None, "low"),
                   "po_number": _f(None, "low")},
        "lines": [
            {"index": 0, "ref": _f("MO-HDAI-28/00"), "lot": _f("7011879919"),
             "qty": _f(None), "unit_price": _f(650), "wasted": _f(False)},
        ],
        "freight": _f(None, "low"),
        "grand_total": _f(None, "low"),
    }
    summary = assemble_and_persist(ticket, vision, labels)
    assert summary["line_count"] == 1
    rows = db.lines_for_ticket(ticket["ticket_id"])
    assert len(rows) == 1
    assert rows[0]["ref"] == "MO-HDAI-28/00"
    assert rows[0]["unit_price"] == 650
