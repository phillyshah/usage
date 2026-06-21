"""Tests for the version + diagnostics endpoints added in v1.1.x."""
from fastapi.testclient import TestClient

from app.main import app
from app.version import VERSION

client = TestClient(app)


def test_version_endpoint():
    r = client.get("/version")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == VERSION
    assert isinstance(body["changelog"], list) and body["changelog"]
    # Newest entry first, with the fields the UI renders.
    top = body["changelog"][0]
    assert {"version", "date", "notes"} <= set(top)
    assert isinstance(top["notes"], list)


def test_diag_reports_offline_in_tests():
    # The test suite runs in OFFLINE_MODE (conftest), so no Supabase key role.
    r = client.get("/diag")
    assert r.status_code == 200
    body = r.json()
    assert body["datastore"] == "offline"
    assert "key_role" not in body  # only present when backed by Supabase
