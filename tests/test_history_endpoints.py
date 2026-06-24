"""HTTP-level tests for the History-tab endpoints (app/main.py)."""
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.db import db, new_id
from app.learning.ingest_reference import load_bundled_masters
from app.main import app
from app.pipeline.assemble import assemble_and_persist

client = TestClient(app)


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
    load_bundled_masters()
    batch = db.create_batch()
    ticket = db.create_ticket({
        "batch_id": batch["id"], "entity": "Maxx Health",
        "source_filename": "MH.jpg", "status": "pending_review"})
    vision = {"header": {}, "grand_total": _f(968),
              "lines": [{"unit_price": _f(900)}, {"unit_price": _f(68)}]}
    assemble_and_persist(ticket, vision, _labels())
    return ticket


def test_auto_resolve_daily_endpoint():
    _seed_ticket()
    r = client.get("/metrics/auto-resolve-daily")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)

    today = datetime.now(timezone.utc).date().isoformat()
    entry = next((e for e in body if e["date"] == today), None)
    assert entry is not None
    assert {"date", "pct_confident", "fields", "confident"} <= set(entry)


def test_learning_endpoint():
    r = client.get("/metrics/learning")
    assert r.status_code == 200
    body = r.json()
    assert {"cumulative", "daily"} <= set(body)
    assert {"prices", "part_descriptions", "reps", "gtin_links"} <= set(
        body["cumulative"])
    assert isinstance(body["daily"], list)
    for item in body["daily"]:
        assert {"date", "corrections_made", "blanks_filled", "low_conf_fixed",
                "facts_learned"} <= set(item)


def test_corrections_uploads_endpoint_newest_first():
    db.log_corrected_upload({"sheets_processed": 1, "tickets_matched": 1,
                             "tickets_unknown": 0, "status": "older"})
    marker = "newest-" + new_id()[:8]
    db.log_corrected_upload({"sheets_processed": 2, "tickets_matched": 2,
                             "tickets_unknown": 1, "status": marker})

    r = client.get("/corrections/uploads")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list) and body

    first = body[0]
    assert {"uploaded_at", "sheets_processed", "tickets_matched",
            "tickets_unknown", "status"} <= set(first)
    # The most recently logged upload is first (newest-first ordering).
    assert first["status"] == marker

    # uploaded_at descending across the list.
    stamps = [row["uploaded_at"] for row in body]
    assert stamps == sorted(stamps, reverse=True)
