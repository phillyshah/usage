"""Handwritten price parsing + grand-total reconciliation confidence."""
import pytest

from app.db import db
from app.learning.ingest_reference import load_bundled_masters
from app.pipeline.assemble import _money, assemble_and_persist


@pytest.mark.parametrize("raw,expected", [
    ("$1,900.00", 1900.0),
    ("1,900", 1900.0),
    ("$68", 68.0),
    ("68.50", 68.5),
    ("900", 900.0),
    (900, 900.0),
    (900.0, 900.0),
    ("$0", 0.0),
    ("($900)", -900.0),
    ("", None),
    ("Ø", None),
    ("N/C", None),
    (None, None),
    (True, None),
])
def test_money_parsing(raw, expected):
    assert _money(raw) == expected


def _f(v, c="high"):
    return {"value": v, "confidence": c}


def _two_line_vision(p1, p2, grand):
    return {"header": {}, "grand_total": _f(grand),
            "lines": [{"unit_price": _f(p1)}, {"unit_price": _f(p2)}]}


def _labels():
    return [
        {"gtin": "00810008120088", "lot": "S41122707", "expiry": "2028-10-31",
         "mfg": "2023-11-01", "ref": "MO-MSFC-56/MH", "serial": None,
         "raw": "x", "decoded": True},
        {"gtin": "00810008121849", "lot": "U37142706", "expiry": "2030-11-30",
         "mfg": "2025-12-01", "ref": "MO-SWCC-65/30", "serial": None,
         "raw": "x", "decoded": True},
    ]


def _price_conf(ticket_id):
    return [f["confidence"] for f in db.field_extractions_for_ticket(ticket_id)
            if f.get("field_name") == "unit_price"]


def _ticket():
    load_bundled_masters()
    b = db.create_batch()
    return db.create_ticket({"batch_id": b["id"], "entity": "Maxx Health",
                             "source_filename": "MH.jpg", "status": "pending_review"})


def test_dollar_formatted_prices_are_parsed_and_total():
    """A '$' / comma price string still becomes a number and a line total."""
    t = _ticket()
    assemble_and_persist(t, _two_line_vision("$900", "$1,900.00", 2800), _labels())
    lines = sorted(db.lines_for_ticket(t["ticket_id"]),
                   key=lambda x: x.get("created_at") or "")
    assert {l["unit_price"] for l in lines} == {900.0, 1900.0}
    assert {l["line_total"] for l in lines} == {900.0, 1900.0}


def test_reconciled_prices_boosted_to_high():
    """Line prices summing to the handwritten Grand Total validates them -> high."""
    t = _ticket()
    assemble_and_persist(t, _two_line_vision("$900", "$68", 968), _labels())
    assert all(c == "high" for c in _price_conf(t["ticket_id"]))
    ticket = db.get_ticket(t["ticket_id"])
    assert not any("Grand total" in f for f in (ticket.get("flags") or []))


def test_mismatched_total_flags_and_does_not_boost():
    t = _ticket()
    assemble_and_persist(t, _two_line_vision("$900", "$68", 5000), _labels())
    ticket = db.get_ticket(t["ticket_id"])
    assert any("Grand total" in f for f in (ticket.get("flags") or []))
    assert all(c != "high" for c in _price_conf(t["ticket_id"]))
