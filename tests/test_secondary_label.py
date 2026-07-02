"""Regression test: secondary partner billing labels (e.g. UNIKO) are picked up.

The scenario: a ticket has 4 barcoded Maxx implant lines AND a UNIKO billing
label pasted on the side. The UNIKO label has no GS1 barcode — it appears only
in the vision result (vlines[4]). The pipeline must:
  1. Pad the barcode list so it's as long as vlines (run.py padding loop).
  2. In the 5th assemble iteration, use the vision-read REF ("UKI0201-L") since
     the barcode slot is empty.
  3. Resolve it through partner_parts.lookup() -> description + part_type.

The test exercises the assemble path directly (no image / no API call needed).
"""
from unittest.mock import patch

import pytest

from app.db import db
from app.learning.ingest_reference import load_bundled_masters
from app.pipeline.assemble import assemble_and_persist


@pytest.fixture(autouse=True)
def _seed_masters():
    load_bundled_masters()


def _f(val, conf="high"):
    return {"value": val, "confidence": conf}


# Four decoded barcodes (Maxx implant stickers)
FOUR_BARCODES = [
    {"gtin": "00811787021906", "ref": "UPUUX831-K", "lot": "R08052703",
     "expiry": "2027-03-31", "mfg": "2022-04-01", "decoded": True, "raw": "..."},
    {"gtin": "00810008124680", "ref": "UFCRLD00-GK", "lot": "U20012717",
     "expiry": "2030-06-30", "mfg": "2025-07-01", "decoded": True, "raw": "..."},
    {"gtin": "00810008124994", "ref": "MTUUX300-GK", "lot": "U20022705",
     "expiry": "2030-05-31", "mfg": "2025-06-01", "decoded": True, "raw": "..."},
    {"gtin": "00840333915196", "ref": "MLMCLD311-K", "lot": "TS1032783",
     "expiry": "2029-12-31", "mfg": "2025-01-01", "decoded": True, "raw": "..."},
]

# Vision returns 5 lines: 4 Maxx + UNIKO appended at end (as the prompt instructs)
VISION_RESULT = {
    "header": {
        "entity":       _f("Maxx Orthopedics"),
        "rep":          _f("O.R.F"),
        "rep_code":     _f("Rv-MO-007"),
        "surgeon":      _f("Dr. Biggs S"),
        "hospital":     _f("Seaside"),
        "surgery_date": _f("2026-06-01"),
        "po_number":    _f(None, "low"),
    },
    "lines": [
        {"index": 0, "ref": _f("UPUUX831-K"),  "lot": _f("R08052703"), "qty": _f(None), "unit_price": _f(2500), "wasted": _f(False)},
        {"index": 1, "ref": _f("UFCRLD00-GK"), "lot": _f("U20012717"), "qty": _f(None), "unit_price": _f(500),  "wasted": _f(False)},
        {"index": 2, "ref": _f("MTUUX300-GK"), "lot": _f("U20022705"), "qty": _f(None), "unit_price": _f(None, "low"), "wasted": _f(False)},
        {"index": 3, "ref": _f("MLMCLD311-K"), "lot": _f("TS1032783"), "qty": _f(None), "unit_price": _f(None, "low"), "wasted": _f(False)},
        # UNIKO secondary billing label — appended AFTER the 4 Maxx lines
        {"index": 4, "ref": _f("UKI0201-L", "high"), "lot": _f(None), "qty": _f(None), "unit_price": _f(None, "low"), "wasted": _f(False)},
    ],
    "freight": _f(None, "low"),
    "grand_total": _f(3000),
}


def _make_ticket():
    batch = db.create_batch()
    return db.create_ticket({
        "batch_id": batch["id"],
        "source_filename": "MO-test.jpg",
        "status": "pending_review",
    })


def test_uniko_secondary_label_produces_line():
    """UNIKO billing label (vision-only, no barcode) generates an output row
    with the correct description and part_type resolved via partner_parts."""
    ticket = _make_ticket()

    # Simulate the run.py padding: 4 barcodes, 5 vlines -> append one empty slot
    labels = list(FOUR_BARCODES)
    vlines = VISION_RESULT["lines"]
    while len(labels) < len(vlines):
        labels.append({"gtin": None, "lot": None, "expiry": None, "mfg": None,
                        "serial": None, "raw": None, "decoded": False, "ref": None})

    assert len(labels) == 5, "Padding must produce 5 labels"

    summary = assemble_and_persist(ticket, VISION_RESULT, labels)
    assert summary["line_count"] == 5

    rows = db.lines_for_ticket(ticket["ticket_id"])
    assert len(rows) == 5, f"Expected 5 rows, got {len(rows)}"

    uniko_row = rows[4]
    assert uniko_row["ref"] == "UKI0201-L", f"REF mismatch: {uniko_row['ref']}"
    # description resolved via partner_parts.lookup (no DB row needed)
    assert uniko_row["description"] == "UNIKO PointCloud Knee Instrument kit - Left"
    # no lot was provided for this label
    assert uniko_row["lot"] is None


def test_uniko_secondary_label_does_not_displace_maxx_lines():
    """The 4 Maxx implant rows must keep their own REFs (UNIKO doesn't shift them)."""
    ticket = _make_ticket()

    labels = list(FOUR_BARCODES)
    vlines = VISION_RESULT["lines"]
    while len(labels) < len(vlines):
        labels.append({"gtin": None, "lot": None, "expiry": None, "mfg": None,
                        "serial": None, "raw": None, "decoded": False, "ref": None})

    assemble_and_persist(ticket, VISION_RESULT, labels)
    rows = db.lines_for_ticket(ticket["ticket_id"])

    expected_refs = ["UPUUX831-K", "UFCRLD00-GK", "MTUUX300-GK", "MLMCLD311-K", "UKI0201-L"]
    for i, exp_ref in enumerate(expected_refs):
        assert rows[i]["ref"] == exp_ref, (
            f"Row {i}: expected {exp_ref}, got {rows[i]['ref']}"
        )
