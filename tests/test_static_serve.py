"""Static UI is served with Cache-Control: no-cache so redeploys aren't hidden
by browser cache; normal API routes are not forced no-cache."""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_index_served_no_cache_with_history_tab():
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-cache"
    body = resp.text
    assert 'id="tab-history"' in body
    assert 'id="panel-history"' in body


def test_app_js_no_cache():
    resp = client.get("/js/app.js")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-cache"


def test_styles_css_no_cache():
    resp = client.get("/css/styles.css")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-cache"


def test_api_route_not_forced_no_cache():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") != "no-cache"
