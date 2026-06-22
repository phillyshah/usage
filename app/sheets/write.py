"""Write the color-coded review workbook (openpyxl).

Sheets: Usage (the flat, one-row-per-line deliverable the accountants work in),
Tickets (header-level reconciliation), Legend. Per the confidence model the cell
colour is driven entirely by the persisted ``field_extractions`` confidence:
    high   -> no fill (confident)
    medium -> amber FFF2CC (low-confidence guess, eyeball it)
    low    -> red   F4CCCC, cell left BLANK (no confident read, human fills)
Keys (Ticket ID, Line ID, File) and Notes stay uncolored.
"""
from __future__ import annotations

import calendar
import io
import os
from datetime import date, datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.db import db

AMBER = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")
RED = PatternFill(start_color="F4CCCC", end_color="F4CCCC", fill_type="solid")
HEADER_FILL = PatternFill(start_color="E8EEF1", end_color="E8EEF1", fill_type="solid")
HEADER_FONT = Font(bold=True)

# (column header, field_name or None for uncolored key/flags)
TICKET_COLUMNS = [
    ("Ticket ID", None),
    ("Source Image", None),
    ("Entity", "entity"),
    ("Surgery Date", "surgery_date"),
    ("Sales Rep / Distributor", "rep"),
    ("Rep/Distributor Code", "rep_code"),
    ("Surgeon", "surgeon"),
    ("Hospital", "hospital"),
    ("PO Number", "po_number"),
    ("Freight/Delivery Fee", "freight"),
    ("Grand Total", "grand_total"),
    ("Sum of Line Totals", "sum_line_totals"),
    ("Flags / Notes", None),
]

# The flat deliverable. Each tuple: (header, kind, field).
#   kind "key"     -> stable id, uncolored
#   kind "file"    -> source photo file name, uncolored
#   kind "ticket"  -> ticket header field (confidence keyed on line_id=None)
#   kind "month"/"year" -> derived from the ticket's surgery_date
#   kind "line"    -> line item field (confidence keyed on the line_id)
#   kind "notes"   -> merged flags, uncolored
USAGE_COLUMNS = [
    ("Ticket ID", "key", "ticket_id"),
    ("Line ID", "key", "line_id"),
    ("File", "file", None),
    ("Reload Code", "ticket", "rep_code"),
    ("Surgeon Name", "ticket", "surgeon"),
    ("Distributor Code", "ticket", "rep_code"),
    ("Surgery Date", "ticket", "surgery_date"),
    ("Surgery Month", "month", "surgery_date"),
    ("Year", "year", "surgery_date"),
    ("Hospital Name", "ticket", "hospital"),
    ("Quantity", "line", "qty"),
    ("Price", "line", "unit_price"),
    ("Lot Number", "line", "lot"),
    ("Reference Number", "line", "ref"),
    ("Expiration Date", "line", "expiry_date"),
    ("Notes", "notes", None),
]


def _parse_date(v):
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _month_name(v):
    d = _parse_date(v)
    return calendar.month_name[d.month] if d else None


def _year(v):
    d = _parse_date(v)
    return d.year if d else None


def _file_stem(name):
    """'MO083596.jpg' -> 'MO083596'. Returns None for empty."""
    if not name:
        return None
    base = os.path.basename(str(name))
    stem = base.rsplit(".", 1)[0] if "." in base else base
    return stem or None


def _conf_map(ticket_id: str) -> dict:
    """(line_id_or_None, field_name) -> confidence for one ticket."""
    out: dict = {}
    for fe in db.field_extractions_for_ticket(ticket_id):
        out[(fe.get("line_id"), fe.get("field_name"))] = (fe.get("confidence") or "low").lower()
    return out


def _fill_for(conf: str):
    if conf == "medium":
        return AMBER
    if conf == "low":
        return RED
    return None


def _style_header(ws, ncols: int) -> None:
    for c in range(1, ncols + 1):
        cell = ws.cell(row=1, column=c)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"


def _autosize(ws, ncols: int) -> None:
    for c in range(1, ncols + 1):
        letter = get_column_letter(c)
        maxlen = 10
        for row in ws.iter_rows(min_col=c, max_col=c):
            for cell in row:
                if cell.value is not None:
                    maxlen = max(maxlen, len(str(cell.value)))
        ws.column_dimensions[letter].width = min(maxlen + 2, 48)


def _flags_text(obj) -> str:
    flags = obj.get("flags") or []
    return "; ".join(flags) if isinstance(flags, list) else str(flags)


def _write_usage_sheet(ws, tickets) -> None:
    """The flat, one-row-per-line deliverable the accountants work in."""
    ws.append([h for h, _, _ in USAGE_COLUMNS])
    _style_header(ws, len(USAGE_COLUMNS))

    for ticket in tickets:
        cmap = _conf_map(ticket["ticket_id"])
        lines = db.lines_for_ticket(ticket["ticket_id"])
        lines.sort(key=lambda x: x.get("created_at") or "")
        ticket_flags = _flags_text(ticket)

        for i, line in enumerate(lines):
            values: list = []
            fills: list = []  # confidence per column (None where uncolored)
            for header, kind, field in USAGE_COLUMNS:
                conf = None
                if kind == "key":
                    val = ticket["ticket_id"] if field == "ticket_id" else line["line_id"]
                elif kind == "file":
                    val = _file_stem(ticket.get("source_filename"))
                elif kind == "notes":
                    parts = [_flags_text(line)]
                    if i == 0 and ticket_flags:  # ticket notes once, on the first line
                        parts.append(ticket_flags)
                    val = "; ".join(p for p in parts if p)
                else:
                    if kind == "line":
                        conf = cmap.get((line["line_id"], field), "low")
                    else:  # ticket / month / year all key on the ticket header field
                        conf = cmap.get((None, field), "low")
                    if conf == "low":
                        val = ""
                    elif kind == "month":
                        val = _month_name(ticket.get(field))
                    elif kind == "year":
                        val = _year(ticket.get(field))
                    elif kind == "line":
                        val = line.get(field)
                    else:
                        val = ticket.get(field)
                values.append(val)
                fills.append(conf)
            ws.append(values)
            r = ws.max_row
            for idx, conf in enumerate(fills, start=1):
                fill = _fill_for(conf) if conf else None
                if fill:
                    ws.cell(row=r, column=idx).fill = fill

    _autosize(ws, len(USAGE_COLUMNS))


def write_review_workbook(batch_id: str) -> bytes:
    wb = Workbook()

    tickets = db.tickets_for_batch(batch_id)
    tickets.sort(key=lambda t: t.get("created_at") or "")

    # ---- Sheet 1: Usage (the deliverable) ----
    ws_u = wb.active
    ws_u.title = "Usage"
    _write_usage_sheet(ws_u, tickets)

    # ---- Sheet 2: Tickets (header-level reconciliation + round-trip) ----
    ws_t = wb.create_sheet("Tickets")
    ws_t.append([h for h, _ in TICKET_COLUMNS])
    _style_header(ws_t, len(TICKET_COLUMNS))
    for ticket in tickets:
        cmap = _conf_map(ticket["ticket_id"])
        row_vals = []
        for header, field in TICKET_COLUMNS:
            if header == "Ticket ID":
                row_vals.append(ticket["ticket_id"])
            elif header == "Source Image":
                row_vals.append(_file_stem(ticket.get("source_filename")) or "")
            elif header == "Flags / Notes":
                row_vals.append(_flags_text(ticket))
            else:
                conf = cmap.get((None, field), "low")
                row_vals.append("" if conf == "low" else ticket.get(field))
        ws_t.append(row_vals)
        r = ws_t.max_row
        for idx, (header, field) in enumerate(TICKET_COLUMNS, start=1):
            if field is None:
                continue
            fill = _fill_for(cmap.get((None, field), "low"))
            if fill:
                ws_t.cell(row=r, column=idx).fill = fill
    _autosize(ws_t, len(TICKET_COLUMNS))

    # ---- Sheet 3: Legend ----
    ws_g = wb.create_sheet("Legend")
    ws_g.append(["Color", "Meaning", "What to do"])
    _style_header(ws_g, 3)
    legend = [
        ("(white / no fill)", "Confident — validated or agreed across sources", "Nothing — trust it", None),
        ("Amber", "Low-confidence guess — single source or a minor disagreement", "Eyeball it; fix if wrong", AMBER),
        ("Red", "Blank / unreadable — no confident read", "Fill it in", RED),
    ]
    for color, meaning, todo, fill in legend:
        ws_g.append([color, meaning, todo])
        if fill:
            ws_g.cell(row=ws_g.max_row, column=1).fill = fill
    ws_g.append([])
    ws_g.append(["Note", "Ticket ID and Line ID are stable keys — do not edit them.", ""])
    ws_g.append(["", "Edit values directly in the colored cells, save, and re-upload.", ""])
    _autosize(ws_g, 3)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
