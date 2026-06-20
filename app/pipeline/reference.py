"""REF / lot resolution against the reference log + learning overrides.

Resolution order honours the spec:
  * REF -> description/size via part_resolved (learned correction overrides log).
  * Recover a missing REF from a known LOT, or from the GTIN->REF crosswalk.
  * Validate against the log; anything absent gets flagged upstream.
"""
from __future__ import annotations

from app.db import db


def _split_size(description: str | None) -> str | None:
    """Pull a size out of a description like 'Tibial Augment, Size 4'."""
    if not description:
        return None
    low = description.lower()
    if "size" in low:
        idx = low.rindex("size")
        tail = description[idx + len("size"):].strip(" :,-")
        return tail or None
    return None


def resolve_part(ref: str | None, gtin: str | None, lot: str | None) -> dict:
    """Look up description/size; recover REF from lot or GTIN crosswalk; validate.

    -> {ref, description, size, expiry_ref, in_log, source}
       source: how the REF was established (printed | lot | gtin_xref | None)
    """
    result = {
        "ref": ref,
        "description": None,
        "size": None,
        "expiry_ref": None,
        "in_log": False,
        "source": "printed" if ref else None,
    }

    # 1. Recover REF if we don't have one.
    if not result["ref"] and lot:
        lot_row = db.lot_lookup(lot)
        if lot_row and lot_row.get("part_no"):
            result["ref"] = lot_row["part_no"]
            result["source"] = "lot"
    if not result["ref"] and gtin:
        xref = db.ref_for_gtin(gtin)
        if xref:
            result["ref"] = xref
            result["source"] = "gtin_xref"

    # 2. Description/size via part_resolved (learned override, else log).
    if result["ref"]:
        part = db.resolve_part_desc(result["ref"])
        if part:
            result["in_log"] = True
            result["description"] = part.get("description")
            result["size"] = part.get("size") or _split_size(part.get("description"))

    # 3. Authoritative expiry for the lot (independent cross-check vs barcode).
    if lot:
        lot_row = db.lot_lookup(lot)
        if lot_row:
            result["expiry_ref"] = lot_row.get("expiry_date")

    return result
