"""Handwritten line items — the pins, screws and instruments written straight
onto the form's own blanks instead of carrying a peel-off label.

Built from a real Maxx Orthopedics ticket: five barcoded implants down the right
side, three hand-written pin lines down the left, and a circled grand total that
only reconciles if both kinds are counted.

    implants  1300 + 2800 + 1240 + 900 + 250 = 6490
    pins        50 +   75 +   50            =  175
    grand total                               6665   <- as written on the ticket

Every one of those pin lines used to vanish, and for three separate reasons:
the prompt never asked for them, the aligner had nowhere to put them, and their
part numbers are not in the implant master.
"""
import pytest

from app.db import db
from app.pipeline.align import align_vision_lines
from app.pipeline.assemble import assemble_and_persist
from app.pipeline.vision import SYSTEM_PROMPT


def _f(value, confidence="high"):
    return {"value": value, "confidence": confidence}


def _label(ref, lot, gtin):
    return {"gtin": gtin, "ref": ref, "lot": lot, "expiry": None, "mfg": None,
            "serial": None, "raw": "...", "decoded": True}


IMPLANTS = [
    ("UFCRLF00-K", "T25012769", "00811767020213", 1300),
    ("RTUUX700-RK", "V10162703", "00811767024303", 2800),
    ("RSUU1120-RK", "U10202739", "00811767024440", 1240),
    ("MLMCLF711-K", "V07032749", "00840333916735", 900),
    ("UPUUX834-K", "U28052710", "00811767021913", 250),
]
# (ref, description, qty, unit price) — the Price box on the form shows the LINE
# total, e.g. "(x2) 25ea" against "Price: $50.00".
PINS = [
    ("MF-DHXX00D", "short headed pins", 2, 25),
    ("MF-DAXX00F", "long pins", 3, 25),
    ("MF-DAXX00F", "threaded pins", 2, 25),
]


def _ticket_with_both_kinds():
    batch = db.create_batch()
    ticket = db.create_ticket({
        "batch_id": batch["id"], "entity": "Maxx Orthopedics",
        "source_filename": "MO18711-A.jpg", "surgeon": "Miller",
        "rep_code": "JS-MO-001", "hospital": "Health First Viera",
        "surgery_date": "2026-09-28", "status": "pending_review",
    })
    labels = [_label(r, l, g) for r, l, g, _ in IMPLANTS]
    lines = [
        {"index": i, "ref": _f(r), "lot": _f(l), "qty": _f(None),
         "unit_price": _f(price), "wasted": _f(False)}
        for i, (r, l, _g, price) in enumerate(IMPLANTS)
    ] + [
        {"index": 5 + i, "ref": _f(r), "lot": _f(None, "low"), "qty": _f(q),
         "unit_price": _f(unit), "wasted": _f(False), "description": _f(desc)}
        for i, (r, desc, q, unit) in enumerate(PINS)
    ]
    vision = {
        "header": {"entity": _f("Maxx Orthopedics"), "surgeon": _f("Miller"),
                   "rep_code": _f("JS-MO-001"), "hospital": _f("Health First Viera"),
                   "surgery_date": _f("2026-09-28"), "rep": _f("Jake Shaw"),
                   "po_number": _f(None, "low")},
        "lines": lines, "freight": _f(None, "low"), "grand_total": _f(6665),
    }
    assemble_and_persist(ticket, vision, labels)
    return ticket["ticket_id"]


# --------------------------------------------------------------------------
def test_the_aligner_keeps_vision_lines_that_have_no_label():
    """align_vision_lines returns one entry per LABEL, so anything past the
    barcode count is dropped. With five labels and eight vision lines that threw
    away all three handwritten ones before assembly ever saw them."""
    labels = [_label(r, l, g) for r, l, g, _ in IMPLANTS]
    vlines = [{"index": i, "lot": _f(l), "ref": _f(r)}
              for i, (r, l, _g, _p) in enumerate(IMPLANTS)]
    vlines += [{"index": 5 + i, "ref": _f(r), "lot": _f(None, "low")}
               for i, (r, _d, _q, _u) in enumerate(PINS)]

    assert len(align_vision_lines(labels, vlines)) == 5, "unpadded, by design"
    padded = labels + [{}, {}, {}]
    aligned = align_vision_lines(padded, vlines)
    assert len(aligned) == 8
    # the three extras landed on the padded slots, in order
    refs = [(v.get("ref") or {}).get("value") for v in aligned[5:]]
    assert refs == [r for r, _d, _q, _u in PINS]


def test_all_eight_lines_reach_the_workbook():
    ticket_id = _ticket_with_both_kinds()
    rows = db.lines_for_ticket(ticket_id)
    assert len(rows) == 8, f"expected 5 implants + 3 pins, got {len(rows)}"
    refs = {r["ref"] for r in rows}
    assert {"MF-DHXX00D", "MF-DAXX00F"} <= refs


def test_a_handwritten_description_survives_an_unknown_part_number():
    """Pins and instruments trail the implant master permanently, so the words
    on the form are the only description there will ever be."""
    db.replace_reference_part_info([])           # master knows nothing
    ticket_id = _ticket_with_both_kinds()
    rows = {r["ref"]: r for r in db.lines_for_ticket(ticket_id)}
    pin = rows["MF-DHXX00D"]
    assert pin["description"] == "short headed pins"
    # and it kept the number as written, XX and all — that is the real
    # catalogue number, not a placeholder
    assert pin["ref"] == "MF-DHXX00D"


def test_the_quantity_and_unit_price_reconcile_to_the_grand_total():
    """'(x2) 25ea' with 'Price: $50.00' is two at 25, not one at 50 — and the
    ticket's own arithmetic is the check: get it wrong and the line total no
    longer meets the circled figure."""
    ticket_id = _ticket_with_both_kinds()
    rows = db.lines_for_ticket(ticket_id)
    pins = [r for r in rows if str(r["ref"]).startswith("MF-")]
    assert sorted(r["qty"] for r in pins) == [2, 2, 3]
    assert all(r["unit_price"] == 25 for r in pins)
    total = sum((r["unit_price"] or 0) * (r["qty"] or 1) for r in rows)
    assert total == 6665


def test_an_unlabelled_line_is_not_credited_to_a_barcode():
    """A padded slot carries no gtin/lot, so the pin lines must not inherit an
    implant's identity."""
    ticket_id = _ticket_with_both_kinds()
    for row in db.lines_for_ticket(ticket_id):
        if str(row["ref"]).startswith("MF-"):
            assert not row.get("gtin")
            assert not row.get("lot")


@pytest.mark.parametrize("phrase", [
    "HANDWRITTEN LINES",          # the section exists at all
    "MF-DHXX00D",                 # a real example, so the shape is unambiguous
    "LINE TOTAL",                 # the qty/price trap is spelled out
])
def test_the_prompt_asks_for_them(phrase):
    assert phrase in SYSTEM_PROMPT
