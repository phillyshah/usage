"""The content-based barcode↔vision alignment (app/pipeline/align.py).

Production bug this guards against: the barcode libraries return labels in
decode order (DataMatrix first, then linear) while vision reads top-to-bottom,
so index pairing attached prices to the wrong implant. Alignment must re-pair
by exact LOT, then exact REF, and only fall back to position.
"""
import pytest

from app.db import db
from app.learning.ingest_reference import load_bundled_masters
from app.pipeline.align import align_vision_lines
from app.pipeline.assemble import assemble_and_persist


@pytest.fixture(autouse=True)
def _seed_masters():
    load_bundled_masters()


def _f(val, conf="high"):
    return {"value": val, "confidence": conf}


def _vline(ref, lot, price, idx=0):
    return {"index": idx, "ref": _f(ref), "lot": _f(lot), "qty": _f(None),
            "unit_price": _f(price) if price is not None else _f(None, "low"),
            "wasted": _f(False)}


def _label(ref, lot, gtin="00811767021906"):
    return {"gtin": gtin, "ref": ref, "lot": lot, "expiry": None, "mfg": None,
            "serial": None, "raw": "...", "decoded": True}


def _empty_label():
    return {"gtin": None, "lot": None, "expiry": None, "mfg": None,
            "serial": None, "raw": None, "decoded": False, "ref": None}


def test_identity_when_order_matches():
    labels = [_label("UPUUX831-K", "LOTA"), _label("MTUUX300-GK", "LOTB")]
    vlines = [_vline("UPUUX831-K", "LOTA", 100), _vline("MTUUX300-GK", "LOTB", 200)]
    aligned = align_vision_lines(labels, vlines)
    assert aligned[0]["lot"]["value"] == "LOTA"
    assert aligned[1]["lot"]["value"] == "LOTB"


def test_lot_match_when_barcode_order_shuffled():
    # Barcodes in decode order B, A — vision reads A, B (top-to-bottom).
    labels = [_label("MTUUX300-GK", "LOTB"), _label("UPUUX831-K", "LOTA")]
    vlines = [_vline("UPUUX831-K", "LOTA", 1900), _vline("MTUUX300-GK", "LOTB", 650)]
    aligned = align_vision_lines(labels, vlines)
    assert aligned[0]["unit_price"]["value"] == 650    # LOTB's price
    assert aligned[1]["unit_price"]["value"] == 1900   # LOTA's price


def test_ref_match_when_vision_lot_unreadable():
    labels = [_label("MTUUX300-GK", "LOTB"), _label("UPUUX831-K", "LOTA")]
    vlines = [_vline("UPUUX831-K", None, 1900), _vline("MTUUX300-GK", None, 650)]
    aligned = align_vision_lines(labels, vlines)
    assert aligned[0]["unit_price"]["value"] == 650
    assert aligned[1]["unit_price"]["value"] == 1900


def test_gtin_sku_match_via_enrichment():
    # Label carries only a GTIN; caller enriched it with the resolved SKU.
    label = _label(None, None)
    label["_sku"] = "UPUUX831-K"
    labels = [_label("MTUUX300-GK", "LOTB"), label]
    vlines = [_vline("UPUUX831-K", None, 1900), _vline("MTUUX300-GK", "LOTB", 650)]
    aligned = align_vision_lines(labels, vlines)
    assert aligned[0]["unit_price"]["value"] == 650
    assert aligned[1]["unit_price"]["value"] == 1900


def test_duplicate_ref_different_lots_pair_by_lot():
    labels = [_label("UPUUX831-K", "LOT2"), _label("UPUUX831-K", "LOT1")]
    vlines = [_vline("UPUUX831-K", "LOT1", 100), _vline("UPUUX831-K", "LOT2", 200)]
    aligned = align_vision_lines(labels, vlines)
    assert aligned[0]["unit_price"]["value"] == 200   # LOT2
    assert aligned[1]["unit_price"]["value"] == 100   # LOT1


def test_misread_lot_falls_back_positionally():
    # Vision misread both lots -> no exact match anywhere -> positional, in order.
    labels = [_label("REFA", "LOTA"), _label("REFB", "LOTB")]
    vlines = [_vline("XREFA", "XLOTA", 100), _vline("XREFB", "XLOTB", 200)]
    aligned = align_vision_lines(labels, vlines)
    assert aligned[0]["unit_price"]["value"] == 100
    assert aligned[1]["unit_price"]["value"] == 200


def test_partner_label_lands_on_padded_slot():
    # 2 barcodes + padded empty label; 3 vlines with the partner label last.
    labels = [_label("UPUUX831-K", "LOTA"), _label("MTUUX300-GK", "LOTB"),
              _empty_label()]
    vlines = [_vline("MTUUX300-GK", "LOTB", 650), _vline("UPUUX831-K", "LOTA", 1900),
              _vline("UKI0201-L", None, 500)]
    aligned = align_vision_lines(labels, vlines)
    assert aligned[0]["ref"]["value"] == "UPUUX831-K"
    assert aligned[1]["ref"]["value"] == "MTUUX300-GK"
    assert aligned[2]["ref"]["value"] == "UKI0201-L"


def test_extra_labels_get_empty_vline():
    labels = [_label("UPUUX831-K", "LOTA"), _label("MTUUX300-GK", "LOTB")]
    vlines = [_vline("MTUUX300-GK", "LOTB", 650)]
    aligned = align_vision_lines(labels, vlines)
    assert aligned[0] == {}
    assert aligned[1]["unit_price"]["value"] == 650


def test_assemble_end_to_end_prices_follow_lots():
    """The production failure: decode order != reading order. After the fix,
    each persisted row's price must be the one written beside ITS label."""
    batch = db.create_batch()
    ticket = db.create_ticket({
        "batch_id": batch["id"],
        "source_filename": "shuffled-test.jpg",
        "status": "pending_review",
    })
    # Decode order: patella first, femoral head second.
    labels = [
        {"gtin": "00811767021906", "ref": "UPUUX831-K", "lot": "U37052721",
         "expiry": "2030-10-31", "mfg": "2025-11-01", "serial": None,
         "raw": "...", "decoded": True},
        {"gtin": "00810008121047", "ref": "MO-HDAI-28/00", "lot": "7011879919",
         "expiry": "2030-05-31", "mfg": "2025-06-01", "serial": None,
         "raw": "...", "decoded": True},
    ]
    # Vision reading order: femoral head first ($1900), patella second ($450).
    vision = {
        "header": {"entity": _f("Maxx Orthopedics"), "rep": _f(None, "low"),
                   "rep_code": _f(None, "low"), "surgeon": _f(None, "low"),
                   "hospital": _f(None, "low"), "surgery_date": _f(None, "low"),
                   "po_number": _f(None, "low")},
        "lines": [
            _vline("MO-HDAI-28/00", "7011879919", 1900, 0),
            _vline("UPUUX831-K", "U37052721", 450, 1),
        ],
        "freight": _f(None, "low"),
        "grand_total": _f(None, "low"),
    }
    assemble_and_persist(ticket, vision, labels)
    rows = {r["ref"]: r for r in db.lines_for_ticket(ticket["ticket_id"])}
    assert rows["MO-HDAI-28/00"]["unit_price"] == 1900
    assert rows["UPUUX831-K"]["unit_price"] == 450
