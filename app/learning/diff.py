"""Diff corrected rows vs the stored original snapshot -> corrections_audit.

Only runs when the per-field snapshot (``field_extractions``) still exists for
the ticket — i.e. the original is within the retention window. If it's been
purged we skip silently; harvest already captured the facts.

This is the calibration record: which low-confidence guesses were actually
wrong (was_low_conf) and which blanks got filled (was_blank).
"""
from __future__ import annotations

from app.db import db


def _norm(v) -> str:
    if v is None:
        return ""
    return str(v).strip().lower()


def diff_ticket(corrected: dict) -> int:
    """Compare a corrected ticket against its snapshot. Returns rows audited.

    Returns 0 (skips) if no snapshot remains for the ticket.
    """
    ticket_id = corrected["ticket_id"]
    snapshot = db.field_extractions_for_ticket(ticket_id)
    if not snapshot:
        return 0  # aged out — harvest already got the facts

    # Index snapshot by (line_id, field_name).
    snap = {(fe.get("line_id"), fe.get("field_name")): fe for fe in snapshot}
    audited = 0

    # Ticket-level fields (line_id is None).
    for field in [
        "entity", "surgery_date", "rep", "rep_code", "surgeon",
        "hospital", "po_number", "freight", "grand_total",
    ]:
        if field not in corrected:
            continue
        audited += _audit_one(ticket_id, None, field, corrected.get(field), snap)

    # Line-level fields.
    for line_id, line in (corrected.get("lines") or {}).items():
        if not line_id or str(line_id).startswith("_row"):
            continue
        for field in ["ref", "description", "size", "lot", "qty", "expiry_date", "unit_price", "line_total"]:
            if field not in line:
                continue
            audited += _audit_one(ticket_id, line_id, field, line.get(field), snap)

    return audited


def _audit_one(ticket_id, line_id, field, corrected_value, snap) -> int:
    fe = snap.get((line_id, field))
    if fe is None:
        return 0
    orig_value = fe.get("orig_value")
    orig_conf = (fe.get("confidence") or "low").lower()

    # No change relative to the original confident read -> nothing to record.
    if _norm(orig_value) == _norm(corrected_value):
        return 0

    was_blank = orig_conf == "low" or orig_value in (None, "")
    was_low_conf = orig_conf == "medium"

    # Only audit cells the human actually touched: blanks filled or guesses changed.
    if corrected_value in (None, ""):
        return 0

    db.add_correction_audit({
        "ticket_id": ticket_id,
        "line_id": line_id,
        "field_name": field,
        "orig_value": orig_value,
        "orig_confidence": orig_conf,
        "corrected_value": str(corrected_value),
        "was_blank": bool(was_blank),
        "was_low_conf": bool(was_low_conf),
    })
    return 1
