"""Regression guard for the Storage upload options.

storage3 2.x copies the `upsert` option directly into the `x-upsert` HTTP
header. httpx rejects non-string header values with a TypeError, so `upsert`
must be the string "true" — not the bool True. This test pins that contract so
the v1.1.0 regression (upsert: True) can't come back.
"""
from app.storage import _SupabaseStorage


def test_supabase_upload_passes_string_upsert():
    captured = {}

    class FakeBucket:
        def upload(self, path, data, options):
            captured["options"] = options

    class FakeStorage:
        def from_(self, bucket):
            return FakeBucket()

    class FakeClient:
        storage = FakeStorage()

    s = _SupabaseStorage.__new__(_SupabaseStorage)  # bypass __init__/network
    s.client = FakeClient()
    s.put("reference-logs", "Expiry_Log.xlsx", b"data", "application/octet-stream")

    upsert = captured["options"]["upsert"]
    assert isinstance(upsert, str), "x-upsert header value must be a string for httpx"
    assert upsert == "true"
