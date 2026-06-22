"""The flat 'Usage' deliverable sheet: exact columns, File name, derived
Surgery Month / Year, and that line device fields land in the right columns.
"""
import io

from openpyxl import load_workbook

from app.db import db
from app.pipeline.assemble import assemble_and_persist
from app.sheets.write import write_review_workbook

EXPECTED_HEADERS = [
    "Ticket ID", "Line ID", "File", "Reload Code", "Surgeon Name",
    "Distributor Code", "Surgery Date", "Surgery Month", "Year",
    "Hospital Name", "Quantity", "Price", "Lot Number", "Reference Number",
    "Expiration Date", "Notes",
]


def _f(value, confidence="high"):
    return {"value": value, "confidence": confidence}


def _empty_label():
    return {"gtin": None, "lot": None, "expiry": None, "mfg": None,
            "serial": None, "raw": None, "decoded": False, "ref": None}


def test_usage_sheet_columns_file_and_derived_month_year():
    db.replace_reference(
        lots=[{"part_no": "USG-REF-1", "description": "Widget", "lot": "L900",
               "expiry_date": "2030-05-31"}],
        parts=[{"part_no": "USG-REF-1", "description": "Widget", "size": None}],
    )
    batch = db.create_batch()
    ticket = db.create_ticket({
        "batch_id": batch["id"],
        "entity": "Maxx Orthopedics",
        "source_filename": "MO083596.jpg",
        "status": "pending_review",
    })
    vision = {
        "header": {
            "surgeon": _f("Woodworth"),
            "rep_code": _f("GR-ME-001"),
            "hospital": _f("Sierra"),
            "surgery_date": _f("2026-06-01"),
        },
        "lines": [{"index": 0, "ref": _f("USG-REF-1"), "lot": _f("L900"),
                   "qty": _f(1), "unit_price": _f(600)}],
        "freight": _f(None, "low"),
        "grand_total": _f(600),
    }
    assemble_and_persist(ticket, vision, [_empty_label()])

    wb = load_workbook(io.BytesIO(write_review_workbook(batch["id"])))
    assert wb.sheetnames == ["Usage", "Tickets", "Legend"]

    ws = wb["Usage"]
    headers = [c.value for c in ws[1]]
    assert headers == EXPECTED_HEADERS

    row = {h: ws.cell(row=2, column=i + 1).value for i, h in enumerate(headers)}
    assert row["File"] == "MO083596"                  # extension stripped
    assert row["Reload Code"] == "GR-ME-001"
    assert row["Distributor Code"] == "GR-ME-001"     # pre-filled from same code
    assert row["Surgeon Name"] == "Woodworth"
    assert row["Hospital Name"] == "Sierra"
    assert row["Surgery Month"] == "June"             # derived from 2026-06-01
    assert row["Year"] == 2026                        # derived
    assert row["Reference Number"] == "USG-REF-1"
    assert row["Lot Number"] == "L900"
    assert row["Quantity"] == 1
    assert row["Price"] == 600


def test_usage_sheet_blank_file_when_unnamed():
    batch = db.create_batch()
    ticket = db.create_ticket({"batch_id": batch["id"], "status": "pending_review"})
    vision = {"header": {}, "lines": [{"index": 0, "qty": _f(1), "unit_price": _f(5)}],
              "freight": _f(None, "low"), "grand_total": _f(5)}
    assemble_and_persist(ticket, vision, [_empty_label()])

    wb = load_workbook(io.BytesIO(write_review_workbook(batch["id"])))
    ws = wb["Usage"]
    headers = [c.value for c in ws[1]]
    file_val = ws.cell(row=2, column=headers.index("File") + 1).value
    assert file_val in (None, "")
