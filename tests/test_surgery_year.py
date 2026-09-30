"""No ticket is from a prior year (app/pipeline/confidence.correct_surgery_year).

The business runs on the current year, so a 2024 on a 2026 form is a slip of
the pen or a misread digit. One real ticket read "Sept. 28, 2024" while its own
patient sticker gave a DOS of 9/28/2026.

The rule is deliberately NOT "force the current year". A surgery on 28 December
processed on 3 January is genuinely from the prior year, and stamping this year
on it would move it eleven months into the future — turning a correct date into
a wrong one, which is worse than the problem being fixed. That case has its own
test below, because it is the one that will actually happen and the one a naive
implementation gets wrong.
"""
import io
from datetime import date

from openpyxl import load_workbook

from app.db import db
from app.pipeline.assemble import assemble_and_persist
from app.pipeline.confidence import correct_surgery_year
from app.sheets.write import write_review_workbook

TODAY = date(2026, 9, 30)
AMBER = "FFF2CC"


def _f(value, confidence="high"):
    return {"value": value, "confidence": confidence}


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------
def test_a_prior_year_is_pulled_forward():
    assert correct_surgery_year("2024-09-28", TODAY) == ("2026-09-28", "2024-09-28")


def test_a_correct_date_is_left_alone():
    """No correction means no note and no amber — don't cry wolf."""
    assert correct_surgery_year("2026-09-28", TODAY) == ("2026-09-28", None)


def test_a_december_surgery_run_in_january_keeps_its_year():
    """THE case a naive "force current year" gets wrong: 28 Dec processed on
    3 Jan is eight days ago, not eleven months from now."""
    assert correct_surgery_year("2026-12-28", date(2027, 1, 3)) == ("2026-12-28", None)


def test_a_date_slightly_ahead_of_today_is_not_treated_as_last_year():
    """A ticket written the day before surgery is not a mistake."""
    assert correct_surgery_year("2026-10-05", TODAY) == ("2026-10-05", None)


def test_a_year_typed_into_the_future_is_also_corrected():
    """The same misread that produces 2024 can produce 2028."""
    assert correct_surgery_year("2028-09-28", TODAY) == ("2026-09-28", "2028-09-28")


def test_an_unreadable_date_is_passed_through_untouched():
    assert correct_surgery_year("not a date", TODAY) == ("not a date", None)
    assert correct_surgery_year(None, TODAY) == (None, None)


def test_the_twenty_ninth_of_february_is_left_alone():
    """Guessing which way to nudge it is worse than a date somebody can read."""
    assert correct_surgery_year("2024-02-29", date(2026, 6, 1)) == ("2024-02-29", None)


# ---------------------------------------------------------------------------
# End to end: the workbook shows the corrected date, amber, with the reason
# ---------------------------------------------------------------------------
def _run(surgery_date):
    batch = db.create_batch()
    t = db.create_ticket({"batch_id": batch["id"], "source_filename": "MO-year.jpg",
                          "status": "pending_review"})
    summary = assemble_and_persist(db.get_ticket(t["ticket_id"]), {
        "header": {"surgeon": _f("Chase"), "surgery_date": _f(surgery_date)},
        "lines": [], "freight": _f(None, "low"), "grand_total": _f(None, "low"),
    }, [])
    ws = load_workbook(io.BytesIO(write_review_workbook(batch["id"])))["Tickets"]
    headers = [c.value for c in ws[1]]
    cell = ws.cell(row=2, column=headers.index("Surgery Date") + 1)
    return summary, cell


def test_the_corrected_year_reaches_the_workbook():
    prior = f"{TODAY.year - 2}-09-28"
    summary, cell = _run(prior)
    assert cell.value == f"09/28/{TODAY.year}"
    assert str(cell.fill.start_color.rgb).endswith(AMBER), \
        "we changed a number the model read — somebody confirms it"


def test_the_correction_says_what_it_changed_and_why():
    """Silently rewriting extracted data is how a tool stops being trusted."""
    prior = f"{TODAY.year - 2}-09-28"
    summary, _ = _run(prior)
    note = " ".join(summary["flags"])
    assert f"09/28/{TODAY.year - 2}" in note, "the original reading has to be recoverable"
    assert f"09/28/{TODAY.year}" in note
    assert "prior year" in note


def test_a_date_needing_no_correction_raises_no_note():
    summary, _ = _run(f"{TODAY.year}-09-28")
    assert not any("corrected to" in f for f in summary["flags"])


def test_the_derived_month_and_year_columns_follow_the_correction():
    """Date, Month and Year all derive from surgery_date — they must not
    disagree with the value the correction produced."""
    batch = db.create_batch()
    t = db.create_ticket({"batch_id": batch["id"], "source_filename": "MO-cols.jpg",
                          "status": "pending_review"})
    assemble_and_persist(db.get_ticket(t["ticket_id"]), {
        "header": {"surgery_date": _f(f"{TODAY.year - 2}-09-28")},
        "lines": [{"index": 0, "ref": _f("YR-REF-1"), "qty": _f(1), "unit_price": _f(10)}],
        "freight": _f(None, "low"), "grand_total": _f(10),
    }, [{"gtin": None, "lot": None, "expiry": None, "mfg": None, "serial": None,
         "raw": None, "decoded": False, "ref": None}])

    ws = load_workbook(io.BytesIO(write_review_workbook(batch["id"])))["Usage"]
    headers = [c.value for c in ws[1]]
    row = {h: ws.cell(row=2, column=i + 1).value for i, h in enumerate(headers)}
    assert row["Year"] == TODAY.year
    assert row["Month"] == 9
    assert row["Date"] == f"09/28/{TODAY.year}"


def test_the_corrected_date_no_longer_trips_the_sanity_flag():
    """validate_ticket sees the fixed value, not the one that was read."""
    summary, _ = _run(f"{TODAY.year - 2}-09-28")
    assert not any("outside sane range" in f for f in summary["flags"])
