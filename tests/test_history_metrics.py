"""Tests for the History-tab backend metric helpers (app/metrics.py + db.py).

The OFFLINE store is a shared singleton across the whole suite, so these tests
assert on the rows they create (by unique id/value) or on before/after deltas —
never on absolute totals of the entire store.
"""
import re
from datetime import datetime, timedelta, timezone

from app import metrics
from app.db import db, new_id
from app.learning.ingest_reference import load_bundled_masters
from app.pipeline.assemble import assemble_and_persist

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _f(v, c="high"):
    return {"value": v, "confidence": c}


def _labels():
    return [
        {"gtin": "00810008120088", "lot": "S41122707", "expiry": "2028-10-31",
         "mfg": "2023-11-01", "ref": "MO-MSFC-56/MH", "serial": None,
         "raw": "x", "decoded": True},
        {"gtin": "00810008121849", "lot": "U37142706", "expiry": "2030-11-30",
         "mfg": "2025-12-01", "ref": "MO-SWCC-65/30", "serial": None,
         "raw": "x", "decoded": True},
    ]


def _seed_ticket():
    """A processed ticket created today, with a mix of high/low confidence."""
    load_bundled_masters()
    batch = db.create_batch()
    ticket = db.create_ticket({
        "batch_id": batch["id"], "entity": "Maxx Health",
        "source_filename": "MH.jpg", "status": "pending_review"})
    # Mismatched grand total so the prices stay low-confidence -> a real mix.
    vision = {"header": {}, "grand_total": _f(5000),
              "lines": [{"unit_price": _f(900, "low")}, {"unit_price": _f(68)}]}
    assemble_and_persist(ticket, vision, _labels())
    return ticket


def test_auto_resolve_by_day_shape_and_pct():
    _seed_ticket()
    rows = metrics.auto_resolve_by_day(14)
    assert isinstance(rows, list)

    today = _today()
    entry = next((r for r in rows if r["date"] == today), None)
    assert entry is not None, "today's ticket should produce a daily entry"

    assert DATE_RE.match(entry["date"])
    assert 0 <= entry["pct_confident"] <= 100
    assert entry["fields"] >= entry["confident"] >= 0
    assert entry["pct_confident"] == round(
        100.0 * entry["confident"] / entry["fields"], 1)

    # Ascending by date.
    dates = [r["date"] for r in rows]
    assert dates == sorted(dates)


def test_corrections_by_day_counts_delta():
    today = _today()
    before = metrics.corrections_by_day(14).get(
        today, {"corrections_made": 0, "blanks_filled": 0, "low_conf_fixed": 0})

    tid = new_id()
    db.add_correction_audit({"ticket_id": tid, "field_name": "unit_price",
                             "was_blank": True, "was_low_conf": False,
                             "corrected_value": "900"})
    db.add_correction_audit({"ticket_id": tid, "field_name": "rep_name",
                             "was_blank": True, "was_low_conf": False,
                             "corrected_value": "Montijo"})
    db.add_correction_audit({"ticket_id": tid, "field_name": "qty",
                             "was_blank": False, "was_low_conf": True,
                             "corrected_value": "3"})

    after = metrics.corrections_by_day(14)[today]
    assert after["corrections_made"] - before["corrections_made"] == 3
    assert after["blanks_filled"] - before["blanks_filled"] == 2
    assert after["low_conf_fixed"] - before["low_conf_fixed"] == 1


def test_corrections_by_day_excludes_old_rows():
    old_dt = (datetime.now(timezone.utc) - timedelta(days=60))
    old_day = old_dt.date().isoformat()
    db.backend.insert("corrections_audit", {
        "id": new_id(), "ticket_id": new_id(), "field_name": "unit_price",
        "was_blank": True, "was_low_conf": False, "corrected_value": "1",
        "corrected_at": old_dt.isoformat()})

    by_day = metrics.corrections_by_day(14)
    assert old_day not in by_day


def test_facts_learned_by_day_delta():
    today = _today()
    before = metrics.facts_learned_by_day(14).get(
        today, {"prices": 0, "part_descriptions": 0, "reps": 0, "gtin_links": 0})

    uniq = new_id()[:8]
    db.learn_price(f"REF-{uniq}", "Hospital X", 900.0)
    db.learn_rep(f"MC-{uniq}", "Montijo")

    after = metrics.facts_learned_by_day(14)[today]
    assert after["prices"] - before["prices"] == 1
    assert after["reps"] - before["reps"] == 1


def test_learning_totals_increase_on_new_gtin():
    before = metrics.learning_totals()["gtin_links"]
    # A brand-new (16-digit) gtin so the upsert creates a fresh row.
    gtin = "9" + new_id().replace("-", "")[:15]
    db.learn_gtin_xref(gtin, "MO-MSFC-56/MH")
    after = metrics.learning_totals()["gtin_links"]
    assert after - before == 1


def test_learning_timeline_structure():
    # Seed something so 'daily' is non-empty.
    db.add_correction_audit({"ticket_id": new_id(), "field_name": "unit_price",
                             "was_blank": True, "was_low_conf": False,
                             "corrected_value": "5"})

    tl = metrics.learning_timeline(14)
    assert set(tl) == {"cumulative", "daily"}

    cumulative = tl["cumulative"]
    assert {"prices", "part_descriptions", "reps", "gtin_links"} <= set(cumulative)

    daily = tl["daily"]
    assert isinstance(daily, list) and daily
    # Newest-first: dates descending.
    dates = [d["date"] for d in daily]
    assert dates == sorted(dates, reverse=True)

    for item in daily:
        assert DATE_RE.match(item["date"])
        assert {"corrections_made", "blanks_filled", "low_conf_fixed",
                "facts_learned"} <= set(item)
        assert {"prices", "part_descriptions", "reps", "gtin_links"} <= set(
            item["facts_learned"])
