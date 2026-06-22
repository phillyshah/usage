"""Reference-log fallback: when the barcode fails, an OCR-read REF or LOT must
still resolve the description/size from the Expiry Log.

This is the v1.2.0 fix for "everything device-related is blank" — barcode decode
is unreliable on phone photos, so vision reads the printed REF/LOT and the
reference log fills in the rest.
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


def test_ocr_ref_resolves_description_from_log():
    db.replace_reference(
        lots=[{"part_no": "FBTRAY-04-RK", "description": "Freedom Knee Tibial Tray, Size 4",
               "lot": "S24182708", "expiry_date": "2028-07-31"}],
        parts=[{"part_no": "FBTRAY-04-RK", "description": "Freedom Knee Tibial Tray, Size 4",
                "size": "4"}],
    )
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

    lines = db.lines_for_ticket(ticket["ticket_id"])
    assert len(lines) == 1
    line = lines[0]
    assert line["ref"] == "FBTRAY-04-RK"
    assert "Freedom Knee" in (line["description"] or "")
    assert line["size"] == "4"
    assert line["lot"] == "S24182708"

    # OCR-read but confirmed in the log -> medium (amber: eyeball it), not blank.
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
    db.replace_reference(
        lots=[{"part_no": "KNOWN-1", "description": "A thing", "lot": "L1",
               "expiry_date": "2030-01-01"}],
        parts=[{"part_no": "KNOWN-1", "description": "A thing", "size": None}],
    )
    ticket = db.create_ticket({"batch_id": "bC", "status": "pending_review"})
    vision = {
        "header": {},
        "lines": [{"index": 0, "ref": _f("NOT-IN-LOG-XYZ"), "lot": _f(None, "low"),
                   "qty": _f(1), "unit_price": _f(100)}],
        "freight": _f(None, "low"),
        "grand_total": _f(100),
    }
    assemble_and_persist(ticket, vision, [_empty_label()])

    line = db.lines_for_ticket(ticket["ticket_id"])[0]
    # REF echoed through, but no log match -> description blank, low confidence.
    assert line["ref"] == "NOT-IN-LOG-XYZ"
    assert (line["description"] or "") == ""
    cmap = _line_conf(ticket["ticket_id"], line["line_id"])
    assert cmap["description"] == "low"
