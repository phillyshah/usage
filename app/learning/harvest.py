"""Harvest ground-truth facts from corrected rows into the learning stores.

Always works — even after the retention window expires — because each corrected
row is self-contained (it already holds REF, description, size, hospital, price,
rep, code). Self-contained and idempotent: re-harvesting the same facts is
harmless.

  Corrected value          -> learning store       (key)
  Description / Size        -> learning_part_desc   (REF)
  Rep name                  -> learning_rep_map     (Rep/Distributor Code)
  Unit Price                -> learning_price        (REF + Hospital)
  REF (with decoded GTIN)   -> learning_gtin_xref   (GTIN)
"""
from __future__ import annotations

from app.db import db


def _num(v):
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def harvest_ticket(corrected: dict) -> dict:
    """Harvest one corrected ticket record (from sheets.read.parse_corrected_workbook).

    Returns counts of what was learned.
    """
    counts = {"part_desc": 0, "rep": 0, "price": 0, "gtin_xref": 0}

    hospital = corrected.get("hospital")
    rep = corrected.get("rep")
    rep_code = corrected.get("rep_code")

    if rep_code and rep:
        db.learn_rep(str(rep_code).strip(), str(rep).strip())
        counts["rep"] += 1

    for line in (corrected.get("lines") or {}).values():
        ref = line.get("ref")
        if not ref:
            continue
        ref = str(ref).strip()

        desc = line.get("description")
        size = line.get("size")
        if desc or size:
            db.learn_part_desc(ref, desc, size)
            counts["part_desc"] += 1

        price = _num(line.get("unit_price"))
        if price is not None and hospital:
            db.learn_price(ref, str(hospital).strip(), price)
            counts["price"] += 1

        # GTIN->REF crosswalk: only when the original line decoded a GTIN.
        gtin = line.get("gtin")
        if not gtin and line.get("line_id"):
            # corrected sheets don't carry GTIN; recover it from the stored line.
            for stored in db.lines_for_ticket(corrected["ticket_id"]):
                if stored.get("line_id") == line.get("line_id") and stored.get("gtin"):
                    gtin = stored["gtin"]
                    break
        if gtin:
            db.learn_gtin_xref(str(gtin).strip(), ref)
            counts["gtin_xref"] += 1

    return counts
