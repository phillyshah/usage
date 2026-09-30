"""Harvest ground-truth facts from corrected rows into the learning stores.

Always works — even after the retention window expires — because each corrected
row is self-contained (it already holds REF, description, size, hospital, price,
rep, code). Self-contained and idempotent: re-harvesting the same facts is
harmless.

  Corrected value          -> learning store        (key)
  Description / Size        -> learning_part_desc    (REF)
  Rep name                  -> learning_rep_map      (Rep/Distributor Code)
  Unit Price                -> learning_price         (REF + Hospital)
                               ...unless the price is unchanged from one this
                               tool suggested, in which case nothing is learned
  REF (with decoded GTIN)   -> learning_gtin_xref    (GTIN)
  Surgeon / Hospital        -> learning_surgeon_map  (<SurgeonLastName><DistCode>)
"""
from __future__ import annotations

from app.db import db
from app.learning.ingest_reference import surgeon_key


# Prices the TOOL proposed. A suggestion that comes back untouched is the tool
# agreeing with itself, not a person deciding anything, and learning it as a
# correction would let the next ticket cite it as ground truth. Step 5 already
# refuses to learn its own rose estimates for this reason; extraction-time
# suggestions need the same refusal, and needed it the moment they started
# filling cells that used to come back blank.
_SUGGESTED_SOURCES = {
    "learned_price", "learned_price_fuzzy", "learned_price_cross",
    "grand_total_residual",
}


def _untouched_suggestions(ticket_id: str) -> dict:
    """line_id -> the price this tool suggested, for lines where it suggested one."""
    out: dict = {}
    if not ticket_id:
        return out
    for fe in db.field_extractions_for_ticket(ticket_id):
        if fe.get("field_name") != "unit_price":
            continue
        if (fe.get("source") or "") not in _SUGGESTED_SOURCES:
            continue
        val = _num(fe.get("orig_value"))
        if val is not None:
            out[fe.get("line_id")] = val
    return out


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
    counts = {"part_desc": 0, "rep": 0, "price": 0, "gtin_xref": 0,
              "surgeon_map": 0, "suggestions_skipped": 0}
    suggested = _untouched_suggestions(corrected.get("ticket_id"))

    hospital = corrected.get("hospital")
    rep = corrected.get("rep")
    rep_code = corrected.get("rep_code")
    surgeon = corrected.get("surgeon")

    if rep_code and rep:
        db.learn_rep(str(rep_code).strip(), str(rep).strip())
        counts["rep"] += 1

    # Surgeon chain: <SurgeonLastName><DistCode> -> surgeon/hospital/dist code,
    # so a corrected header teaches the tool combinations the master lacks.
    if surgeon and rep_code:
        key = surgeon_key(str(surgeon).strip(), str(rep_code).strip())
        if key:
            db.learn_surgeon_map(
                key,
                str(surgeon).strip(),
                str(hospital).strip() if hospital else None,
                str(rep_code).strip(),
            )
            counts["surgeon_map"] += 1

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
            proposed = suggested.get(line.get("line_id"))
            if proposed is not None and abs(proposed - price) < 0.005:
                # Unchanged from what we proposed: nobody asserted anything.
                counts["suggestions_skipped"] += 1
            else:
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
