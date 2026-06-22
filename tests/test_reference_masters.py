"""Reference masters ingest + lookups (GTIN / part_info / surgeon crosswalks)."""
from app.db import db
from app.learning.ingest_reference import (
    load_bundled_masters,
    parse_part_info,
    parse_surgeon_info,
    surgeon_key,
)


def test_bundled_masters_ingest_counts():
    s = load_bundled_masters()
    assert s["gtin_rows"] == 5413
    assert s["part_rows"] == 1719
    assert s["surgeon_rows"] > 500           # ~559 records after overflow skips


def test_part_info_skips_junk_first_row():
    """Row 1 is junk (1,2,3,4); the real header is row 2; data from row 3."""
    csv = (b"1,2,3,4\n"
           b"Part Number,Description,Part Type,Category\n"
           b"ABC-1,A Widget,Screw,Hardware\n")
    rows = parse_part_info(csv)
    assert rows == [{"part_number": "ABC-1", "description": "A Widget",
                     "part_type": "Screw", "category": "Hardware"}]


def test_surgeon_overflow_rows_skipped():
    """A record is any row with a non-empty DistCode; blank-key rows are skipped."""
    csv = ("Surgeon-DistCode,Surgeon Last Name,DistCode,Status,Distributor,"
           "DistributorRep,Sales Manager,Maxx Ortho Manager,Shipping Address,"
           "Surgeon Full Name,Hospital,Region\n"
           "SmithAB-001,Smith,AB-001,Active,Dist,Rep,Mgr,,123 Main St,John Smith,"
           "Mercy Hospital,West\n"
           ",,,,,,,,456 Overflow Ave,,,\n").encode()
    rows = parse_surgeon_info(csv)
    assert len(rows) == 1
    assert rows[0]["surgeon_distcode"] == "SMITHAB-001"
    assert rows[0]["hospital"] == "Mercy Hospital"


def test_surgeon_key_normalizes():
    assert surgeon_key("Montijo", "MC-001") == "MONTIJOMC-001"
    assert surgeon_key("Montijo", " mc - 001 ") == "MONTIJOMC-001"
    assert surgeon_key(None, "MC-001") is None


def test_gtin_and_part_lookups_after_ingest():
    load_bundled_masters()
    g = db.sku_for_gtin("00810008120088")
    assert g and g["sku"] == "MO-MSFC-56/MH" and g["status"] == "In Use"
    # Trailing +/- in REFs are significant and preserved.
    p = db.part_info_for_ref("MO-HDAI-36/40-")
    assert p and p["part_type"] and p["category"]
