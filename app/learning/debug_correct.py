"""Bridge Debug Console inline corrections into the same harvest/diff pipeline
the .xlsx corrections-upload path uses (app/main.py: corrections_upload), so a
Debug Console save and a workbook re-upload teach the learning stores
identically. Additive only — see app/learning/harvest.py, app/learning/diff.py.
"""
from __future__ import annotations

from app.db import db
from app.learning.diff import diff_ticket
from app.learning.harvest import harvest_ticket
from app.pipeline.assemble import LINE_FIELDS, TICKET_FIELDS

_HEADER_FIELDS = [f for f in TICKET_FIELDS if f != "sum_line_totals"]


def build_corrected_shape(ticket_id: str, header_edits: dict | None,
                          line_edits: dict | None) -> dict:
    """Merge user edits over the ticket's CURRENT stored values into the same
    {ticket_id, <header fields>, lines: {line_id: {...}}} shape
    sheets.read.parse_corrected_workbook produces for the xlsx path.

    Untouched fields fall back to the value already stored on the ticket/line
    row, so harvest/diff always see a fully-populated record — not just the
    edited subset. Passing header_edits=None / line_edits=None (the
    "confirm all as correct" case) harvests/diffs the current stored values
    verbatim.
    """
    ticket = db.get_ticket(ticket_id)
    if ticket is None:
        raise ValueError(f"unknown ticket_id {ticket_id}")

    header_edits = header_edits or {}
    corrected: dict = {"ticket_id": ticket_id}
    for field in _HEADER_FIELDS:
        corrected[field] = header_edits.get(field, ticket.get(field))

    lines = db.lines_for_ticket(ticket_id)
    lines.sort(key=lambda r: r.get("created_at") or "")
    line_edits = line_edits or {}
    corrected_lines: dict = {}
    for row in lines:
        line_id = row["line_id"]
        edits = line_edits.get(line_id) or {}
        merged = {"line_id": line_id, "gtin": row.get("gtin")}
        for field in LINE_FIELDS:
            merged[field] = edits.get(field, row.get(field))
        corrected_lines[line_id] = merged
    corrected["lines"] = corrected_lines
    return corrected


def apply_debug_correction(ticket_id: str, header_edits: dict | None = None,
                           line_edits: dict | None = None) -> dict:
    """Harvest + audit one ticket's corrected/confirmed values, mark it verified."""
    corrected = build_corrected_shape(ticket_id, header_edits, line_edits)
    counts = harvest_ticket(corrected)
    audited = diff_ticket(corrected)
    db.update_ticket(ticket_id, {"status": "verified"})
    return {"learned": counts, "audited_fields": audited}
