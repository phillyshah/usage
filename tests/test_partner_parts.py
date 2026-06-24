"""Built-in uniko partner overlay: partner_parts.lookup, db fallback, resolve_part."""
import tempfile

import pytest

import app.db as dbmod
from app import partner_parts
from app.db import db
from app.learning.ingest_reference import load_bundled_masters
from app.pipeline.reference import resolve_part


# ---- partner_parts.lookup -------------------------------------------------

def test_lookup_exact_left():
    row = partner_parts.lookup("UKI0201-L")
    assert row["description"] == "UNIKO PointCloud Knee Instrument kit - Left"
    assert row["part_type"] == "UNIKO"
    assert row["category"] == "PSI Kit"


def test_lookup_case_insensitive_right():
    row = partner_parts.lookup("uki0201-r")
    assert row["description"] == "UNIKO PointCloud Knee Instrument kit - Right"
    assert row["part_type"] == "UNIKO"
    assert row["category"] == "PSI Kit"


@pytest.mark.parametrize("ref", ["ZZZ", None, ""])
def test_lookup_misses_return_none(ref):
    assert partner_parts.lookup(ref) is None


# ---- db.part_info_for_ref overlay fallback (fresh empty backend) ----------

@pytest.fixture
def fresh_db():
    """Point db at a FRESH empty local backend so the overlay path (DB miss ->
    overlay) is exercised in isolation; restore the shared backend after."""
    orig = db.backend
    db.backend = dbmod._LocalBackend(tempfile.mkdtemp())
    try:
        yield db
    finally:
        db.backend = orig


def test_overlay_via_empty_db(fresh_db):
    row = fresh_db.part_info_for_ref("UKI0201-L")
    assert row is not None
    assert row["description"] == "UNIKO PointCloud Knee Instrument kit - Left"
    # Overlay rows are not DB rows -> no ingested_at stamp.
    assert "ingested_at" not in row
    assert fresh_db.part_info_for_ref("nope") is None


# ---- resolve_part: a vision-read uniko REF (gtin=None) resolves -----------

def test_resolve_part_uniko_ref():
    part = resolve_part("UKI0201-R", None, None)
    assert part["in_part_info"] is True
    assert part["description"] == "UNIKO PointCloud Knee Instrument kit - Right"
    assert part["part_type"] == "UNIKO"
    assert part["category"] == "PSI Kit"
    assert part["ref"] == "UKI0201-R"
    assert part["ref_source"] == "printed"


# ---- overlay must NOT shadow real masters ---------------------------------

def test_overlay_does_not_shadow_real_masters():
    load_bundled_masters()
    row = db.part_info_for_ref("MO-HDAI-36/40-")
    assert row is not None
    assert row.get("part_type")
    assert row.get("category")
