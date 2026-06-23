"""Tests for the per-sheet reference masters uploads + freshness reporting.

The suite runs in OFFLINE_MODE (see tests/conftest.py). Each masters upload
full-replaces the target table, so these tests upload their own small fixtures
and assert on the resulting exact counts.
"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


# --- Small valid fixtures, mirroring app/learning/ingest_reference.py parsers ---

GTIN_CSV = (
    "STATUS,GTIN_14,GTIN_12_UPC,PACKAGING_TYPE,PACKAGING_LEVEL,"
    "PRODUCT_DESCRIPTION,SKU\n"
    "Active,00811633020017,811633020017,Box,Each,Widget A,SKU-A\n"
    "Active,00811633020024,811633020024,Box,Each,Widget B,SKU-B\n"
).encode("utf-8")
GTIN_ROWS = 2

# part_info: row 1 is junk, row 2 header, data from row 3.
PART_CSV = (
    "1,2,3,4\n"
    "Part Number,Description,Part Type,Category\n"
    "PN-100,Plate,Implant,Trauma\n"
    "PN-101,Screw,Implant,Trauma\n"
    "PN-102,Driver,Instrument,Trauma\n"
).encode("utf-8")
PART_ROWS = 3

# surgeon_info: header row 1; a record is any row with a non-empty DistCode.
SURGEON_CSV = (
    "Surgeon-DistCode,Surgeon Last Name,DistCode,Status,Distributor,"
    "DistributorRep,Sales Manager,Maxx Ortho Manager,Shipping Address,"
    "Surgeon Full Name,Hospital,Region\n"
    "SmithMC-001,Smith,MC-001,Active,DistCo,Rep1,Mgr1,,Addr,John Smith,"
    "General Hospital,West\n"
)
SURGEON_CSV = SURGEON_CSV.encode("utf-8")
SURGEON_ROWS = 1


def _upload(filename, data, kind=None):
    files = {"files": (filename, data, "text/csv")}
    payload = {"kind": kind} if kind is not None else None
    return client.post("/reference/masters", files=files, data=payload)


def test_kind_gtin_routes_only_gtins():
    r = _upload("anything.csv", GTIN_CSV, kind="gtin")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["gtin_rows"] == GTIN_ROWS
    assert body["part_rows"] is None
    assert body["surgeon_rows"] is None


def test_kind_part_info_routes_only_part_info():
    r = _upload("anything.csv", PART_CSV, kind="part_info")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["part_rows"] == PART_ROWS
    assert body["gtin_rows"] is None
    assert body["surgeon_rows"] is None


def test_kind_surgeon_routes_only_surgeons():
    r = _upload("anything.csv", SURGEON_CSV, kind="surgeon")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["surgeon_rows"] == SURGEON_ROWS
    assert body["gtin_rows"] is None
    assert body["part_rows"] is None


def test_invalid_kind_returns_400():
    r = _upload("anything.csv", GTIN_CSV, kind="bogus")
    assert r.status_code == 400
    assert "detail" in r.json()


def test_filename_routing_still_works_without_kind():
    # No kind -> falls back to filename-substring routing.
    r = _upload("GTIN_Codes.csv", GTIN_CSV)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["gtin_rows"] == GTIN_ROWS
    # Only the GTIN file was supplied, so the others are untouched (None).
    assert body["part_rows"] is None
    assert body["surgeon_rows"] is None


def test_status_reports_per_sheet_masters_freshness():
    # Upload a known small set for each master (full-replace), then read status.
    assert _upload("g.csv", GTIN_CSV, kind="gtin").status_code == 200
    assert _upload("p.csv", PART_CSV, kind="part_info").status_code == 200
    assert _upload("s.csv", SURGEON_CSV, kind="surgeon").status_code == 200

    r = client.get("/reference/status")
    assert r.status_code == 200, r.text
    body = r.json()

    masters = body["masters"]
    for key in ("gtin", "part_info", "surgeon"):
        assert key in masters
        assert "rows" in masters[key]
        assert "updated_at" in masters[key]

    assert masters["gtin"]["rows"] == GTIN_ROWS
    assert masters["part_info"]["rows"] == PART_ROWS
    assert masters["surgeon"]["rows"] == SURGEON_ROWS
    # A populated table reports a non-null timestamp.
    assert masters["gtin"]["updated_at"]

    # The new explicit `log` mirror is present.
    assert "log" in body
    assert "loaded" in body["log"]
