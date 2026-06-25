"""Tests for the learning-integrity safeguard (app/metrics.py + db.py + endpoint).

The OFFLINE store is a shared singleton across the suite, so these tests use
unique fact keys and per-store deltas rather than absolute totals, and restore
any high-water mark they deliberately perturb so later tests aren't poisoned.
"""
from fastapi.testclient import TestClient

from app import metrics
from app.db import db, new_id
from app.main import app

client = TestClient(app)

_HWM = metrics._HWM_PREFIX


def _price_table(health: dict) -> dict:
    return next(t for t in health["tables"] if t["key"] == "prices")


def test_app_setting_round_trip():
    k = f"test_key_{new_id()[:8]}"
    assert db.get_app_setting(k) is None
    db.set_app_setting(k, "42")
    assert db.get_app_setting(k) == "42"
    db.set_app_setting(k, "43")          # upsert updates in place
    assert db.get_app_setting(k) == "43"


def test_bump_only_raises_never_lowers():
    table = "learning_rep_map"
    db.set_app_setting(_HWM + table, "999999")
    metrics.bump_learning_watermarks()
    assert int(db.get_app_setting(_HWM + table)) == 999999
    # Restore the mark to the real current count so we don't leave the store
    # looking permanently "shrunk" for any later reader.
    cur = next(s["count"] for s in metrics._store_stats() if s["table"] == table)
    db.set_app_setting(_HWM + table, str(cur))


def test_health_detects_shrink_and_recovers():
    pn = f"HEALTHX-{new_id()[:8]}"

    # 1. Learn a fact and snapshot the baseline -> intact, not shrunk.
    db.learn_price(pn, "Hospital Z", 100.0)
    metrics.bump_learning_watermarks()
    h1 = metrics.learning_health()
    assert h1["status"] in ("ok", "stale")     # intact (recent activity -> ok)
    assert _price_table(h1)["shrunk"] is False

    # 2. Simulate data loss: drop the row WITHOUT lowering the mark.
    db.backend.delete_where("learning_price", "part_no", pn)
    h2 = metrics.learning_health()
    assert h2["status"] == "at_risk"
    assert _price_table(h2)["shrunk"] is True

    # 3. Re-learn -> count recovers to the mark -> snapshot -> intact again.
    db.learn_price(pn, "Hospital Z", 100.0)
    metrics.bump_learning_watermarks()
    h3 = metrics.learning_health()
    assert _price_table(h3)["shrunk"] is False


def test_health_shape_and_endpoint():
    db.learn_rep(f"RC-{new_id()[:6]}", "Smith")   # ensure the store is non-empty
    metrics.bump_learning_watermarks()

    r = client.get("/metrics/learning/health")
    assert r.status_code == 200
    body = r.json()
    assert {"status", "total", "last_learned", "tables"} <= set(body)
    assert body["status"] in ("ok", "stale", "at_risk", "empty")
    keys = {t["key"] for t in body["tables"]}
    assert {"prices", "part_descriptions", "reps", "gtin_links"} <= keys
    for t in body["tables"]:
        assert {"key", "count", "hwm", "shrunk", "last_learned"} <= set(t)


def test_diag_includes_learning_block():
    r = client.get("/diag")
    assert r.status_code == 200
    body = r.json()
    assert "learning" in body
    assert "status" in body["learning"]
