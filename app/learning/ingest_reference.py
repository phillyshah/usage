"""Reference masters ingest -> full replace of the product/surgeon crosswalks.

Three read-only CSV sources (FIELD_GUIDE §2), each full-replaced on upload:

  * GTIN_Codes.csv   header row 1: STATUS,GTIN_14,GTIN_12_UPC,PACKAGING_TYPE,
                     PACKAGING_LEVEL,PRODUCT_DESCRIPTION,SKU
  * part_info.csv    row 1 is junk (1,2,3,4); real header is row 2
                     (Part Number,Description,Part Type,Category); data from row 3.
  * surgeon_info.csv header row 1; a *record* is any row with a non-empty DistCode.
                     Address-overflow rows have blank keys -> skip them.

Bundled copies live in ``reference/`` so offline/dev and the test suite start warm
without a live upload; the endpoint replaces them from an operator upload.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

from app.db import db

# Repo-bundled defaults (committed; product/surgeon data, no PHI).
REFERENCE_DIR = Path(__file__).resolve().parents[2] / "reference"


def _clean(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip().lstrip("﻿")  # strip stray BOM on first column
    return s or None


def _normalize_distcode(code: str | None) -> str | None:
    """'MC - 001' / ' mc-001 ' -> 'MC-001' (collapse spaces, upper)."""
    if not code:
        return None
    return "".join(str(code).split()).upper() or None


def surgeon_key(last_name: str | None, dist_code: str | None) -> str | None:
    """Build the join key: <SurgeonLastName><DistCode>, normalized upper/no-space."""
    ln = _clean(last_name)
    dc = _normalize_distcode(dist_code)
    if not (ln and dc):
        return None
    return ("".join(ln.split()) + dc).upper()


# ---------------------------------------------------------------------------
# Parsers (bytes -> list[dict] matching the table columns)
# ---------------------------------------------------------------------------
def parse_gtin_codes(data: bytes) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
    for r in reader:
        gtin = _clean(r.get("GTIN_14"))
        if not gtin or gtin in seen:
            continue
        seen.add(gtin)
        rows.append({
            "gtin_14": gtin,
            "gtin_12_upc": _clean(r.get("GTIN_12_UPC")),
            "sku": _clean(r.get("SKU")),
            "product_description": _clean(r.get("PRODUCT_DESCRIPTION")),
            "status": _clean(r.get("STATUS")),
            "packaging_type": _clean(r.get("PACKAGING_TYPE")),
            "packaging_level": _clean(r.get("PACKAGING_LEVEL")),
        })
    return rows


def parse_part_info(data: bytes) -> list[dict]:
    """Row 1 junk, row 2 header, data from row 3. Key = Part Number (exact)."""
    rows: list[dict] = []
    seen: set[str] = set()
    all_rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
    if len(all_rows) < 3:
        return rows
    # all_rows[0] = junk, all_rows[1] = header, all_rows[2:] = data
    for raw in all_rows[2:]:
        if not raw:
            continue
        part = _clean(raw[0] if len(raw) > 0 else None)
        if not part or part in seen:
            continue
        seen.add(part)
        rows.append({
            "part_number": part,
            "description": _clean(raw[1] if len(raw) > 1 else None),
            "part_type": _clean(raw[2] if len(raw) > 2 else None),
            "category": _clean(raw[3] if len(raw) > 3 else None),
        })
    return rows


def parse_surgeon_info(data: bytes) -> list[dict]:
    """A record is any row with a non-empty DistCode; blank-key overflow rows skip."""
    rows: list[dict] = []
    seen: set[str] = set()
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
    for r in reader:
        dist = _normalize_distcode(r.get("DistCode"))
        last = _clean(r.get("Surgeon Last Name"))
        if not dist:
            continue  # address-overflow continuation row
        key = surgeon_key(last, dist)
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append({
            "surgeon_distcode": key,
            "surgeon_last_name": last,
            "dist_code": dist,
            "status": _clean(r.get("Status")),
            "distributor": _clean(r.get("Distributor")),
            "distributor_rep": _clean(r.get("DistributorRep")),
            "sales_manager": _clean(r.get("Sales Manager")),
            "maxx_ortho_manager": _clean(r.get("Maxx Ortho Manager")),
            "surgeon_full_name": _clean(r.get("Surgeon Full Name")),
            "hospital": _clean(r.get("Hospital")),
            "region": _clean(r.get("Region")),
        })
    return rows


# ---------------------------------------------------------------------------
# Ingest orchestration
# ---------------------------------------------------------------------------
def ingest_masters(gtin: bytes | None = None,
                   part_info: bytes | None = None,
                   surgeon_info: bytes | None = None) -> dict:
    """Full-replace whichever masters are supplied. Returns per-table row counts."""
    summary = {"gtin_rows": None, "part_rows": None, "surgeon_rows": None}
    if gtin is not None:
        g = parse_gtin_codes(gtin)
        db.replace_reference_gtin(g)
        summary["gtin_rows"] = len(g)
    if part_info is not None:
        p = parse_part_info(part_info)
        db.replace_reference_part_info(p)
        summary["part_rows"] = len(p)
    if surgeon_info is not None:
        s = parse_surgeon_info(surgeon_info)
        db.replace_reference_surgeons(s)
        summary["surgeon_rows"] = len(s)
    db.log_masters_ingest(summary.copy())
    return summary


def load_bundled_masters() -> dict:
    """Seed the reference masters from the repo-bundled CSVs (offline/dev/tests)."""
    files = {
        "gtin": REFERENCE_DIR / "GTIN_Codes.csv",
        "part_info": REFERENCE_DIR / "part_info.csv",
        "surgeon_info": REFERENCE_DIR / "surgeon_info.csv",
    }
    kwargs = {k: p.read_bytes() for k, p in files.items() if p.exists()}
    if not kwargs:
        return {"gtin_rows": None, "part_rows": None, "surgeon_rows": None}
    return ingest_masters(**kwargs)
