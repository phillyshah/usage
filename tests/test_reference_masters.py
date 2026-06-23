"""Reference masters ingest + lookups (GTIN / part_info / surgeon crosswalks)."""
import io

from openpyxl import Workbook

from app.db import db
from app.learning.ingest_reference import (
    bundled_as_of,
    load_bundled_masters,
    parse_gtin_codes,
    parse_part_info,
    parse_surgeon_info,
    surgeon_key,
)


def _xlsx(rows: list[list]) -> bytes:
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


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


def test_bundled_masters_stamped_with_snapshot_date():
    """The bundled seed records the snapshot's as-of date (reference/MASTERS_VERSION),
    not the load time, so the freshness banner reflects the data date."""
    load_bundled_masters()
    latest = db.latest_masters_ingest()
    assert latest["ingested_at"] == bundled_as_of()
    assert bundled_as_of().startswith("2026-06-23")


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


def test_excel_masters_parse_like_csv():
    """Production masters are Excel; the loader must accept .xlsx too."""
    g = parse_gtin_codes(_xlsx([
        ["STATUS", "GTIN_14", "GTIN_12_UPC", "PACKAGING_TYPE", "PACKAGING_LEVEL",
         "PRODUCT_DESCRIPTION", "SKU"],
        ["In Use", 810008120088, 811, "Regular", "Each", "Shell", "MO-MSFC-56/MH"],
    ]))
    # Excel stores GTIN_14 as a number; leading zeros must be restored to 14.
    assert g[0]["gtin_14"] == "00810008120088"
    assert g[0]["sku"] == "MO-MSFC-56/MH"

    p = parse_part_info(_xlsx([
        [1, 2, 3, 4],
        ["Part Number", "Description", "Part Type", "Category"],
        ["MO-HDAI-36/40-", "Ceramic Head", "Libertas Head", "Head"],
    ]))
    assert p == [{"part_number": "MO-HDAI-36/40-", "description": "Ceramic Head",
                  "part_type": "Libertas Head", "category": "Head"}]

    s = parse_surgeon_info(_xlsx([
        ["Surgeon-DistCode", "Surgeon Last Name", "DistCode", "Status",
         "Surgeon Full Name", "Hospital", "Region"],
        ["MontijoMC-001", "Montijo", "MC-001", "Active", "Harvey Montijo",
         "Wellington", "South"],
        [None, None, None, None, None, None, None],  # address overflow -> skipped
    ]))
    assert len(s) == 1 and s[0]["surgeon_distcode"] == "MONTIJOMC-001"
