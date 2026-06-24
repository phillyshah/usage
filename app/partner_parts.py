"""Built-in partner SKU overlay.

A small, fixed set of partner products that must always resolve to a
description/part type/category — independent of the uploaded reference masters.
The monthly Maxx parts upload FULL-REPLACES ``reference_part_info``, so partner
SKUs that lived only in that table would be wiped each month. Keeping them here
(consulted by ``db.part_info_for_ref`` after the DB lookup misses) means they
resolve everywhere and survive every upload.

Each entry mirrors a ``reference_part_info`` row shape:
``{part_number, description, part_type, category}``.
"""
from __future__ import annotations

# Keyed by exact part number. Add future fixed-partner SKUs here.
PARTNER_PARTS: dict[str, dict] = {
    "UKI0201-L": {
        "part_number": "UKI0201-L",
        "description": "UNIKO PointCloud Knee Instrument kit - Left",
        "part_type": "UNIKO",
        "category": "PSI Kit",
    },
    "UKI0201-R": {
        "part_number": "UKI0201-R",
        "description": "UNIKO PointCloud Knee Instrument kit - Right",
        "part_type": "UNIKO",
        "category": "PSI Kit",
    },
}

# Case-insensitive index for a vision-read REF whose case may differ.
_BY_UPPER = {k.upper(): v for k, v in PARTNER_PARTS.items()}


def lookup(ref: str | None) -> dict | None:
    """Resolve a partner REF -> part_info-shaped row, or None. Exact then CI."""
    if not ref:
        return None
    hit = PARTNER_PARTS.get(ref)
    if hit:
        return hit
    return _BY_UPPER.get(str(ref).strip().upper())
