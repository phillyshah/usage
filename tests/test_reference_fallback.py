"""Resolution fallbacks when the barcode is weak or absent.

Deterministic-first still holds: a decoded GTIN gives the SKU exactly. But on
phone photos the DataMatrix often fails, so a vision-read REF (or just a LOT)
must still resolve the device via the masters:
  * OCR REF  -> part_info description (medium: legible but uncross-checked).
  * OCR LOT  -> Expiry Log recovers the Part No + authoritative expiry.
  * Unknown REF -> echoed but description blank/low (never guessed).
"""
from app.db import db
from app.pipeline.assemble import assemble_and_persist


def _f(value, confidence="high"):
    return {"value": value, "confidence": confidence}


def _empty_label():
    return {"gtin": None, "lot": None, "expiry": None, "mfg": None,
            "serial": None, "raw": None, "decoded": False, "ref": None}


def _line_conf(ticket_id, line_id):
    return {
        fe["field_name"]: fe["confidence"]
        for fe in db.field_extractions_for_ticket(ticket_id)
        if fe.get("line_id") == line_id
    }


def test_ocr_ref_resolves_description_from_part_info():
    db.replace_reference_part_info([
        {"part_number": "FBTRAY-04-RK", "description": "Freedom Knee Tibial Tray, Size 4",
         "part_type": "Freedom Tray", "category": "Tibial Tray"},
    ])
    ticket = db.create_ticket({"batch_id": "bA", "entity": "Maxx Orthopedics",
                               "status": "pending_review"})
    vision = {
        "header": {},
        "lines": [{"index": 0, "ref": _f("FBTRAY-04-RK"), "lot": _f("S24182708"),
                   "qty": _f(1), "unit_price": _f(600)}],
        "freight": _f(None, "low"),
        "grand_total": _f(600),
    }
    assemble_and_persist(ticket, vision, [_empty_label()])

    line = db.lines_for_ticket(ticket["ticket_id"])[0]
    assert line["ref"] == "FBTRAY-04-RK"
    assert "Freedom Knee" in (line["description"] or "")
    assert line["lot"] == "S24182708"

    # OCR-read REF that resolves in part_info -> medium (amber: eyeball it).
    cmap = _line_conf(ticket["ticket_id"], line["line_id"])
    assert cmap["ref"] == "medium"
    assert cmap["description"] == "medium"


def test_ocr_lot_recovers_ref_and_expiry_from_log():
    db.replace_reference(
        lots=[{"part_no": "RSUU2135-RK", "description": "Stem Extension 13.5mm x 75mm",
               "lot": "TA5202733", "expiry_date": "2029-11-30"}],
        parts=[{"part_no": "RSUU2135-RK", "description": "Stem Extension 13.5mm x 75mm",
                "size": None}],
    )
    db.replace_reference_part_info([
        {"part_number": "RSUU2135-RK", "description": "Stem Extension 13.5mm x 75mm",
         "part_type": "Revision Stem", "category": "Stem"},
    ])
    ticket = db.create_ticket({"batch_id": "bB", "status": "pending_review"})
    vision = {
        "header": {},
        # Vision could not read the REF, only the LOT.
        "lines": [{"index": 0, "ref": _f(None, "low"), "lot": _f("TA5202733"),
                   "qty": _f(1), "unit_price": _f(600)}],
        "freight": _f(None, "low"),
        "grand_total": _f(600),
    }
    assemble_and_persist(ticket, vision, [_empty_label()])

    line = db.lines_for_ticket(ticket["ticket_id"])[0]
    assert line["ref"] == "RSUU2135-RK"          # recovered from the lot
    assert "Stem Extension" in (line["description"] or "")
    assert line["expiry_date"] == "2029-11-30"   # authoritative from the log lot


def test_unknown_ref_stays_blank_and_low():
    db.replace_reference_part_info([
        {"part_number": "KNOWN-1", "description": "A thing",
         "part_type": "T", "category": "C"},
    ])
    ticket = db.create_ticket({"batch_id": "bC", "status": "pending_review"})
    vision = {
        "header": {},
        "lines": [{"index": 0, "ref": _f("NOT-IN-MASTER-XYZ"), "lot": _f(None, "low"),
                   "qty": _f(1), "unit_price": _f(100)}],
        "freight": _f(None, "low"),
        "grand_total": _f(100),
    }
    assemble_and_persist(ticket, vision, [_empty_label()])

    line = db.lines_for_ticket(ticket["ticket_id"])[0]
    # REF echoed through, but no part_info match -> description blank, low.
    assert line["ref"] == "NOT-IN-MASTER-XYZ"
    assert (line["description"] or "") == ""
    cmap = _line_conf(ticket["ticket_id"], line["line_id"])
    assert cmap["description"] == "low"


def test_wasted_line_flagged_and_still_counts():
    db.replace_reference_part_info([
        {"part_number": "W-REF-1", "description": "Wasted Widget",
         "part_type": "T", "category": "C"},
    ])
    ticket = db.create_ticket({"batch_id": "bW", "status": "pending_review"})
    vision = {
        "header": {},
        "lines": [{"index": 0, "ref": _f("W-REF-1"), "lot": _f("L1"),
                   "qty": _f(1), "unit_price": _f(200), "wasted": _f(True)}],
        "freight": _f(None, "low"),
        "grand_total": _f(200),
    }
    assemble_and_persist(ticket, vision, [_empty_label()])
    line = db.lines_for_ticket(ticket["ticket_id"])[0]
    assert "WASTED" in (line.get("flags") or [])
    assert line["line_total"] == 200             # wasted price still counts
