"""The learning stores must actually influence extraction (v2.9.0).

Before this release the learning loop was write-only: corrections were
harvested into the learning tables but (rep name aside) nothing read them back
at extraction time, so re-processing after corrections showed no improvement.
"""
import pytest

from app.db import db
from app.learning.harvest import harvest_ticket
from app.learning.ingest_reference import load_bundled_masters, surgeon_key
from app.pipeline.assemble import assemble_and_persist
from app.pipeline.reference import resolve_part, resolve_surgeon


@pytest.fixture(autouse=True)
def _seed_masters():
    load_bundled_masters()


def _f(val, conf="high"):
    return {"value": val, "confidence": conf}


def _make_ticket():
    batch = db.create_batch()
    return db.create_ticket({
        "batch_id": batch["id"],
        "source_filename": "learning-test.jpg",
        "status": "pending_review",
    })


def _vision_one_line(price=None, hospital=None, surgeon=None, rep_code=None,
                     ref="ZZTEST-REF-1", lot="ZZLOT01"):
    return {
        "header": {
            "entity": _f("Maxx Orthopedics"),
            "rep": _f(None, "low"),
            "rep_code": _f(rep_code) if rep_code else _f(None, "low"),
            "surgeon": _f(surgeon) if surgeon else _f(None, "low"),
            "hospital": _f(hospital) if hospital else _f(None, "low"),
            "surgery_date": _f(None, "low"),
            "po_number": _f(None, "low"),
        },
        "lines": [
            {"index": 0, "ref": _f(ref), "lot": _f(lot), "qty": _f(None),
             "unit_price": _f(price) if price is not None else _f(None, "low"),
             "wasted": _f(False)},
        ],
        "freight": _f(None, "low"),
        "grand_total": _f(None, "low"),
    }


ONE_LABEL = [{"gtin": None, "ref": "ZZTEST-REF-1", "lot": "ZZLOT01",
              "expiry": None, "mfg": None, "serial": None,
              "raw": "...", "decoded": True}]


# ---- learned GTIN -> REF crosswalk -----------------------------------------

def test_learned_gtin_xref_recovers_ref():
    db.learn_gtin_xref("00999999990001", "ZZXREF-PART")
    part = resolve_part(None, "00999999990001", None)
    assert part["ref"] == "ZZXREF-PART"
    assert part["ref_source"] == "gtin_learned"
    assert part["in_gtin_master"] is False


# ---- learned description / size ---------------------------------------------

def test_learned_part_desc_fills_description_and_size():
    db.learn_part_desc("ZZDESC-PART", "Test Widget Left", "Size 5")
    part = resolve_part("ZZDESC-PART", None, None)
    assert part["description"] == "Test Widget Left"
    assert part["size"] == "Size 5"
    assert part["desc_source"] == "correction"
    assert part["in_part_info"] is False


# ---- learned price: same-hospital fill only ----------------------------------

def test_learned_price_fills_missing_price_same_hospital():
    db.learn_price("ZZTEST-REF-1", "Mercy General", 725.0)
    ticket = _make_ticket()
    assemble_and_persist(
        ticket, _vision_one_line(price=None, hospital="Mercy General"), list(ONE_LABEL))
    row = db.lines_for_ticket(ticket["ticket_id"])[0]
    assert row["unit_price"] == 725.0
    assert row["line_total"] == 725.0
    assert any("filled from the learned price" in f for f in row["flags"])
    fes = db.field_extractions_for_ticket(ticket["ticket_id"])
    price_fe = [fe for fe in fes if fe.get("field_name") == "unit_price"
                and fe.get("line_id")][0]
    assert price_fe["confidence"] == "medium"


def test_learned_price_does_not_fill_for_other_hospital():
    db.learn_price("ZZTEST-REF-1", "Mercy General", 725.0)
    ticket = _make_ticket()
    assemble_and_persist(
        ticket, _vision_one_line(price=None, hospital="St. Rose Siena"), list(ONE_LABEL))
    row = db.lines_for_ticket(ticket["ticket_id"])[0]
    assert row["unit_price"] is None


def test_learned_price_never_overrides_a_read_price():
    db.learn_price("ZZTEST-REF-1", "Mercy General", 725.0)
    ticket = _make_ticket()
    assemble_and_persist(
        ticket, _vision_one_line(price=900, hospital="Mercy General"), list(ONE_LABEL))
    row = db.lines_for_ticket(ticket["ticket_id"])[0]
    assert row["unit_price"] == 900
    assert any("differs from the learned price" in f for f in row["flags"])


def test_learned_price_hospital_resolved_via_surgeon_map():
    """Handwritten hospital missing, but the learned surgeon map resolves it
    from surgeon+DistCode — the price memory must still key correctly."""
    db.learn_surgeon_map(surgeon_key("Kovacs", "ZZ-001"), "Kovacs",
                         "Mercy General", "ZZ-001")
    db.learn_price("ZZTEST-REF-1", "Mercy General", 725.0)
    ticket = _make_ticket()
    assemble_and_persist(
        ticket,
        _vision_one_line(price=None, surgeon="Kovacs", rep_code="ZZ-001"),
        list(ONE_LABEL))
    row = db.lines_for_ticket(ticket["ticket_id"])[0]
    assert row["unit_price"] == 725.0


# ---- learned surgeon / hospital map ------------------------------------------

def test_harvest_learns_surgeon_map():
    counts = harvest_ticket({
        "ticket_id": "no-such-ticket",
        "surgeon": "Kovacs",
        "rep_code": "ZZ-002",
        "hospital": "Riverview",
        "rep": "Some Rep",
        "lines": {},
    })
    assert counts["surgeon_map"] == 1
    learned = db.learned_surgeon_for_key(surgeon_key("Kovacs", "ZZ-002"))
    assert learned and learned["hospital"] == "Riverview"


def test_resolve_surgeon_learned_fallback():
    db.learn_surgeon_map(surgeon_key("Fallback", "ZZ-003"), "Fallback",
                         "Fallback Hospital", "ZZ-003")
    surg = resolve_surgeon("Fallback", "ZZ-003")
    assert surg["matched"] is True
    assert surg["source"] == "learned"
    assert surg["hospital"] == "Fallback Hospital"


def test_resolve_surgeon_master_wins_over_learned():
    # The same key exists in both the master and the learned map; the master
    # (deterministic reference) must win.
    key = surgeon_key("Masterdoc", "ZZ-004")
    db.backend.upsert("reference_surgeons", ["surgeon_distcode"], {
        "surgeon_distcode": key, "surgeon_last_name": "Masterdoc",
        "dist_code": "ZZ-004", "status": "Active",
        "surgeon_full_name": "Dr. Masterdoc", "hospital": "Master Hospital",
        "region": "East",
    })
    db.learn_surgeon_map(key, "Masterdoc", "Learned Hospital", "ZZ-004")
    surg = resolve_surgeon("Masterdoc", "ZZ-004")
    assert surg["matched"] is True
    assert surg["source"] == "master"
    assert surg["hospital"] == "Master Hospital"
