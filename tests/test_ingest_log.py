"""Tests for the Expiry Log ingest path and its error reporting.

Covers:
* parsing scales to a large (60k-row) workbook in OFFLINE_MODE;
* the real Expiry_Log.xlsx (when present) loads to the expected counts;
* the route translates a Supabase 42501 row-level-security error into an
  operator-actionable message instead of leaking a raw dict.
"""
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.learning.ingest_log import ingest_expiry_log
from app.main import _explain_ingest_error, app

client = TestClient(app)

REAL_FIXTURE = Path(__file__).parent / "fixtures" / "Expiry_Log.xlsx"


def _make_big_log(n_parts: int, lots_per_part: int) -> bytes:
    """Build an Expiry Log with the real layout (title row, spacer, header @ row 3)."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Expiry Log History"
    ws.append(["Expiry Date Log (All History)"])
    ws.append([])
    ws.append(["Part No", "Description", "Lot #", "Total Qty Released",
               "Lot Pallet", "Expiry Date", "Notes"])
    for p in range(n_parts):
        part_no = f"MO-PART-{p:05d}"
        for l in range(lots_per_part):
            ws.append([part_no, f"Device {p} size {l}", f"LOT{p:05d}-{l}",
                       (l + 1), f"PAL{p}", "2027-12-31", None])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_ingest_scales_to_60k_rows():
    data = _make_big_log(n_parts=2000, lots_per_part=30)  # 60,000 rows
    summary = ingest_expiry_log(data)
    assert summary["row_count"] == 60000
    assert summary["unique_parts"] == 2000
    assert summary["unique_lots"] == 60000


def test_route_returns_counts_for_big_log():
    data = _make_big_log(n_parts=100, lots_per_part=10)
    r = client.post(
        "/reference/log",
        files={"file": ("Expiry_Log.xlsx", data,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["row_count"] == 1000
    assert body["unique_parts"] == 100


@pytest.mark.skipif(not REAL_FIXTURE.exists(),
                    reason="real Expiry_Log.xlsx fixture not present")
def test_real_expiry_log_loads_expected_counts():
    data = REAL_FIXTURE.read_bytes()
    summary = ingest_expiry_log(data)
    # The exact counts from the operator's real export.
    assert summary["row_count"] == 63214
    assert summary["unique_parts"] == 1682
    assert summary["unique_lots"] == 51365


def test_42501_error_is_translated_to_actionable_message():
    # Simulate the exact error postgrest raises when the anon key hits RLS.
    err = Exception(
        "{'message': 'new row violates row-level security policy for table "
        "\"reference_lots\"', 'code': '42501', 'hint': None, 'details': None}"
    )
    msg = _explain_ingest_error(err)
    assert "row-level security" in msg
    assert "service_role" in msg
    assert "SUPABASE_SERVICE_KEY" in msg
    # The raw dict must not be the whole story — we add guidance.
    assert "Project Settings" in msg


def test_generic_error_is_passed_through():
    msg = _explain_ingest_error(ValueError("boom"))
    assert "boom" in msg
    assert "row-level security" not in msg
