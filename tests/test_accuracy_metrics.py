"""Which fields humans actually have to fix.

`corrections_audit` has recorded every correction since 2.9.0, and until now the
only thing that read it was a per-day count. These tests pin the grouping that
makes it answer a useful question, and in particular the number that was never
computed: how often the tool was CONFIDENT and wrong.

That one matters because it is the only failure the operator is never told
about. A blank cell is red and a guess is amber; a high-confidence mistake is
plain white, and goes into the books unchallenged.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.db import db
from app.learning import diff as diff_mod
from app.main import app
from app.metrics import AUDITED_FIELDS, correction_accuracy

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clean():
    """Other suites write corrections too, and these tests assert exact counts."""
    db.backend.replace_all("corrections_audit", [], key_col="ticket_id")
    assert db.backend.select("corrections_audit") == []
    yield


def _add(field, confidence, was=None, fixed="x", days_ago=1, n=1):
    """One audited correction, as diff._audit_one would have written it."""
    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    for _ in range(n):
        db.add_correction_audit({
            "ticket_id": "t", "line_id": None, "field_name": field,
            "orig_value": was, "orig_confidence": confidence,
            "corrected_value": fixed,
            "was_blank": confidence == "low",
            "was_low_conf": confidence == "medium",
            "corrected_at": when.isoformat(),
        })


# --------------------------------------------------------------------------
# The three splits
# --------------------------------------------------------------------------
def test_a_high_confidence_correction_is_a_silent_error():
    """diff._audit_one sets was_blank for 'low' and was_low_conf for 'medium',
    which leaves 'high' carrying neither flag — confident, and wrong."""
    _add("unit_price", "high", was="8650", fixed="650")
    r = correction_accuracy()
    field = r["by_field"][0]
    assert (field["silent"], field["amber"], field["blank"]) == (1, 0, 0)
    assert r["silent"] == 1 and r["silent_rate"] == 1.0


def test_medium_is_amber_and_low_is_blank():
    _add("unit_price", "medium", was="8650", fixed="650")
    _add("unit_price", "low", was=None, fixed="650")
    field = correction_accuracy()["by_field"][0]
    assert (field["silent"], field["amber"], field["blank"]) == (0, 1, 1)
    assert correction_accuracy()["silent"] == 0


def test_fields_are_ranked_worst_first():
    """The report exists to say where to spend next, so the field costing the
    most review time has to be the one you read first."""
    _add("hospital", "low", n=3)
    _add("unit_price", "low", n=9)
    _add("lot", "medium", n=5)
    assert [f["field"] for f in correction_accuracy()["by_field"]] == \
        ["unit_price", "lot", "hospital"]


def test_totals_agree_with_the_per_field_splits():
    _add("unit_price", "low", n=4)
    _add("unit_price", "high", n=2)
    _add("ref", "medium", n=3)
    r = correction_accuracy()
    assert r["total"] == 9
    assert sum(f["total"] for f in r["by_field"]) == 9
    assert r["by_confidence"] == {"low": 4, "medium": 3, "high": 2}
    for f in r["by_field"]:
        assert f["blank"] + f["amber"] + f["silent"] == f["total"]


def test_an_unrecognised_confidence_is_treated_as_the_least_trusted():
    """A null or junk confidence must never be counted as 'high' — that would
    inflate the one number the report exists to make honest."""
    _add("ref", "", n=1)
    _add("ref", "nonsense", n=1)
    r = correction_accuracy()
    assert r["silent"] == 0 and r["by_confidence"]["low"] == 2


# --------------------------------------------------------------------------
# Window and examples
# --------------------------------------------------------------------------
def test_corrections_outside_the_window_are_excluded():
    _add("ref", "low", days_ago=200)
    _add("lot", "low", days_ago=2)
    assert [f["field"] for f in correction_accuracy(days=90)["by_field"]] == ["lot"]


def test_examples_prefer_the_silent_ones():
    """A number tells you where to look; a before/after tells you why. The
    unflagged cases are the ones nobody saw, so they lead."""
    _add("unit_price", "low", was=None, fixed="650", n=5)
    _add("unit_price", "high", was="8650", fixed="650")
    examples = correction_accuracy()["by_field"][0]["examples"]
    assert examples[0] == {"was": "8650", "now": "650", "flagged": False}
    assert len(examples) <= 3


def test_long_values_are_truncated():
    _add("description", "low", was=None, fixed="Y" * 200)
    eg = correction_accuracy()["by_field"][0]["examples"][0]
    assert len(eg["now"]) <= 40 and eg["now"].endswith("…")


def test_no_corrections_yet_reports_zeros_rather_than_failing():
    r = correction_accuracy()
    assert r["total"] == 0 and r["silent"] == 0 and r["silent_rate"] == 0.0
    assert r["by_field"] == []


# --------------------------------------------------------------------------
# The PHI boundary
# --------------------------------------------------------------------------
def test_the_report_cannot_name_a_patient_field():
    """corrections_audit can only contain fields diff_ticket compares, and that
    list deliberately excludes patient_initials (docs/WORK_LOG.md). AUDITED_FIELDS
    mirrors it so this test fails if a future edit widens one without the other,
    rather than the report quietly starting to carry patient data."""
    import inspect

    source = inspect.getsource(diff_mod.diff_ticket)
    for field in AUDITED_FIELDS:
        assert f'"{field}"' in source, f"{field} is not audited by diff_ticket"
    assert "patient_initials" not in source
    assert "patient_initials" not in AUDITED_FIELDS


# --------------------------------------------------------------------------
# Route
# --------------------------------------------------------------------------
def test_the_route_returns_the_documented_shape():
    _add("unit_price", "high", was="8650", fixed="650", n=2)
    body = client.get("/metrics/accuracy?days=30").json()
    assert body["days"] == 30
    assert body["silent"] == 2
    assert set(body) == {"days", "total", "silent", "silent_rate",
                         "by_confidence", "by_field"}
    assert set(body["by_field"][0]) == {"field", "total", "blank", "amber",
                                        "silent", "examples"}
