"""Patient initials: the two letters, and the boundary around them.

The initials now come from the one extraction call like every other header
field. They used to need a second API call on a cropped sticker, because the
stored image was masked and assemble never saw the patient area; with the mask
gone that crop is unnecessary.

What EXTRACT_PATIENT_INITIALS controls has therefore changed, and these tests
pin the new boundary: it decides whether the two letters are KEPT, not whether
they are read. Off means the value is discarded before it can reach the
database or the workbook. On means two letters and nothing else survives —
anything longer is rejected rather than truncated into a plausible-looking
answer.
"""
import io
from unittest.mock import patch

from openpyxl import load_workbook

from app.db import db
from app.pipeline.assemble import assemble_and_persist
from app.sheets.write import write_review_workbook


def _f(value, confidence="high"):
    return {"value": value, "confidence": confidence}


def _flag_on():
    return patch("app.pipeline.assemble.settings.extract_patient_initials", True)


def _run(initials, *, conf="high", flag=True, filename="MO-inits.jpg"):
    """Assemble one ticket whose vision header carries these initials."""
    batch = db.create_batch()
    ticket = db.create_ticket({
        "batch_id": batch["id"], "source_filename": filename,
        "status": "pending_review",
    })
    header = {"surgeon": _f("Woodworth"), "rep_code": _f("GR-ME-001"),
              "surgery_date": _f("2026-06-01")}
    if initials is not None or conf is not None:
        header["patient_initials"] = _f(initials, conf)
    ctx = _flag_on() if flag else patch(
        "app.pipeline.assemble.settings.extract_patient_initials", False)
    with ctx:
        assemble_and_persist(db.get_ticket(ticket["ticket_id"]), {
            "header": header,
            "lines": [{"index": 0, "ref": _f("INIT-REF-1"), "qty": _f(1),
                       "unit_price": _f(100)}],
            "freight": _f(None, "low"), "grand_total": _f(100),
        }, [{"gtin": None, "lot": None, "expiry": None, "mfg": None,
             "serial": None, "raw": None, "decoded": False, "ref": None}])
    return batch, db.get_ticket(ticket["ticket_id"])


def _inits_cell(batch):
    ws = load_workbook(io.BytesIO(write_review_workbook(batch["id"])))["Usage"]
    headers = [c.value for c in ws[1]]
    assert headers[3] == "Inits", "Inits sits right after Surgeon"
    return ws.cell(row=2, column=4)


# ---------------------------------------------------------------------------
# The flag
# ---------------------------------------------------------------------------
def test_flag_is_off_by_default():
    from app.config import Settings
    assert Settings(_env_file=None).extract_patient_initials is False


def test_flag_off_discards_the_initials_entirely():
    """The model is shown the sticker either way now, so "off" has to mean the
    value is dropped on our side — not merely that we didn't ask for it."""
    batch, ticket = _run("AB", flag=False)
    assert ticket["patient_initials"] is None
    assert _inits_cell(batch).value is None


def test_flag_on_keeps_the_two_letters():
    batch, ticket = _run("AB")
    assert ticket["patient_initials"] == "AB"
    assert _inits_cell(batch).value == "AB"


# ---------------------------------------------------------------------------
# Two letters, or nothing. The schema is the safety net.
# ---------------------------------------------------------------------------
def test_a_full_name_is_discarded_not_truncated():
    """Truncating "John Doe" to "Jo" would be the worst outcome: a wrong answer
    that looks like a right one."""
    _, ticket = _run("John Doe")
    assert ticket["patient_initials"] is None


def test_a_middle_initial_is_rejected():
    _, ticket = _run("JMD")
    assert ticket["patient_initials"] is None


def test_a_single_letter_is_rejected():
    _, ticket = _run("J")
    assert ticket["patient_initials"] is None


def test_digits_are_rejected():
    """An MRN fragment must never be mistaken for initials."""
    _, ticket = _run("80")
    assert ticket["patient_initials"] is None


def test_lowercase_is_normalised():
    _, ticket = _run("ab")
    assert ticket["patient_initials"] == "AB"


def test_nothing_read_leaves_the_cell_blank():
    _, ticket = _run(None, conf="low")
    assert ticket["patient_initials"] is None


def test_a_failed_extraction_leaves_the_cell_blank():
    """The whole vision result is empty, initials included."""
    batch = db.create_batch()
    ticket = db.create_ticket({
        "batch_id": batch["id"], "source_filename": "MO-fail.jpg",
        "status": "pending_review",
    })
    with _flag_on():
        assemble_and_persist(db.get_ticket(ticket["ticket_id"]), {
            "header": {}, "lines": [], "freight": _f(None, "low"),
            "grand_total": _f(None, "low"), "error": "api down",
        }, [])
    assert db.get_ticket(ticket["ticket_id"])["patient_initials"] is None


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------
def test_a_weak_read_is_amber_not_deleted():
    """A hesitant read of two letters is still worth showing — the reviewer can
    confirm it far faster than they can look it up."""
    batch, ticket = _run("CD", conf="low")
    assert ticket["patient_initials"] == "CD"
    cell = _inits_cell(batch)
    assert cell.value == "CD"
    assert str(cell.fill.start_color.rgb).endswith("FFF2CC")


def test_corrected_workbook_inits_column_round_trips():
    """A corrected workbook carrying the Inits column parses it back out. The
    parser matches on header name, so the column's position doesn't matter."""
    from app.sheets.read import parse_corrected_workbook

    batch, ticket = _run("EF", filename="MO-rt.jpg")
    parsed = parse_corrected_workbook(write_review_workbook(batch["id"]))
    assert parsed["tickets"][ticket["ticket_id"]]["patient_initials"] == "EF"


def test_reprocessing_updates_rather_than_blanks():
    """Reprocessing re-reads the initials from the same image, so the value
    survives a second pass instead of being wiped by the merge."""
    batch = db.create_batch()
    ticket = db.create_ticket({
        "batch_id": batch["id"], "source_filename": "MO-reproc.jpg",
        "status": "pending_review",
    })
    for _ in range(2):
        with _flag_on():
            assemble_and_persist(db.get_ticket(ticket["ticket_id"]), {
                "header": {"surgeon": _f("Nobody"), "surgery_date": _f("2026-06-01"),
                           "patient_initials": _f("CD", "medium")},
                "lines": [], "freight": _f(None, "low"),
                "grand_total": _f(None, "low"),
            }, [])
    assert db.get_ticket(ticket["ticket_id"])["patient_initials"] == "CD"
