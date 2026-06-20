"""Write the color-coded review workbook (openpyxl).

Three sheets: Tickets, Line Items, Legend. Per the confidence model the cell
colour is driven entirely by the persisted ``field_extractions`` confidence:
    high   -> no fill (confident)
    medium -> amber FFF2CC (low-confidence guess, eyeball it)
    low    -> red   F4CCCC, cell left BLANK (no confident read, human fills)
Keys (Ticket ID, Line ID, Source Image) and Flags stay uncolored.
"""
from __future__ import annotations

import io

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

LINE_COLUMNS = [
    ("Ticket ID", None),
    ("Line ID", None),
    ("REF (Part No)", "ref"),
    ("Description", "description"),
    ("Size", "size"),
    ("LOT", "lot"),
    ("Qty", "qty"),
    ("Mfg Date", "mfg_date"),
    ("Expiration Date", "expiry_date"),
    ("Unit Price", "unit_price"),
    ("Line Total", "line_total"),
    ("Flags / Notes", None),
]


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


def write_review_workbook(batch_id: str) -> bytes:
    wb = Workbook()

    # ---- Sheet 1: Tickets ----
    ws_t = wb.active
    ws_t.title = "Tickets"
    ws_t.append([h for h, _ in TICKET_COLUMNS])
    _style_header(ws_t, len(TICKET_COLUMNS))

    # ---- Sheet 2: Line Items ----
    ws_l = wb.create_sheet("Line Items")
    ws_l.append([h for h, _ in LINE_COLUMNS])
    _style_header(ws_l, len(LINE_COLUMNS))

    tickets = db.tickets_for_batch(batch_id)
    tickets.sort(key=lambda t: t.get("created_at") or "")

    for ticket in tickets:
        cmap = _conf_map(ticket["ticket_id"])
        # Tickets row
        row_vals = []
        for header, field in TICKET_COLUMNS:
            if header == "Ticket ID":
                row_vals.append(ticket["ticket_id"])
            elif header == "Source Image":
                row_vals.append(ticket.get("source_image_path") or "")
            elif header == "Flags / Notes":
                flags = ticket.get("flags") or []
                row_vals.append("; ".join(flags) if isinstance(flags, list) else str(flags))
            else:
                conf = cmap.get((None, field), "low")
                # low => blank cell
                row_vals.append("" if conf == "low" else ticket.get(field))
        ws_t.append(row_vals)
        r = ws_t.max_row
        for idx, (header, field) in enumerate(TICKET_COLUMNS, start=1):
            if field is None:
                continue
            fill = _fill_for(cmap.get((None, field), "low"))
            if fill:
                ws_t.cell(row=r, column=idx).fill = fill

        # Line rows
        lines = db.lines_for_ticket(ticket["ticket_id"])
        lines.sort(key=lambda x: x.get("created_at") or "")
        for line in lines:
            lvals = []
            for header, field in LINE_COLUMNS:
                if header == "Ticket ID":
                    lvals.append(ticket["ticket_id"])
                elif header == "Line ID":
                    lvals.append(line["line_id"])
                elif header == "Flags / Notes":
                    flags = line.get("flags") or []
                    lvals.append("; ".join(flags) if isinstance(flags, list) else str(flags))
                else:
                    conf = cmap.get((line["line_id"], field), "low")
                    lvals.append("" if conf == "low" else line.get(field))
            ws_l.append(lvals)
            lr = ws_l.max_row
            for idx, (header, field) in enumerate(LINE_COLUMNS, start=1):
                if field is None:
                    continue
                fill = _fill_for(cmap.get((line["line_id"], field), "low"))
                if fill:
                    ws_l.cell(row=lr, column=idx).fill = fill

    _autosize(ws_t, len(TICKET_COLUMNS))
    _autosize(ws_l, len(LINE_COLUMNS))

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
