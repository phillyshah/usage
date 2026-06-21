"""Unit tests for Supabase key-role detection.

These guard the v1.1.x regression: a publishable/anon key in SUPABASE_SERVICE_KEY
must be detectable so the app can warn early and translate the 42501 RLS error
instead of failing cryptically.
"""
import base64
import json

from app.supabase_key import detect_key_role, is_privileged_key


def _make_jwt(role: str) -> str:
    """Build an unsigned legacy-style Supabase JWT carrying a role claim."""
    def seg(d: dict) -> str:
        raw = json.dumps(d).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    header = seg({"alg": "HS256", "typ": "JWT"})
    payload = seg({"iss": "supabase", "ref": "abc", "role": role})
    return f"{header}.{payload}.signature_not_verified"


def test_legacy_service_role_jwt_is_privileged():
    key = _make_jwt("service_role")
    assert detect_key_role(key) == "service_role"
    assert is_privileged_key(key) is True


def test_legacy_anon_jwt_is_not_privileged():
    key = _make_jwt("anon")
    assert detect_key_role(key) == "anon"
    assert is_privileged_key(key) is False


def test_new_secret_key_is_privileged():
    key = "sb_secret_abcdef+ the rest is opaque"
    assert detect_key_role(key) == "service_role"
    assert is_privileged_key(key) is True


def test_new_publishable_key_is_not_privileged():
    key = "sb_publishable_abcdef"
    assert detect_key_role(key) == "anon"
    assert is_privileged_key(key) is False


def test_empty_and_garbage_keys_are_unknown():
    for key in ["", None, "not-a-jwt", "a.b", "....", "x.y.z"]:
        assert is_privileged_key(key) is False


def test_jwt_with_bad_base64_payload_does_not_raise():
    # Middle segment is not valid base64 JSON -> None, never an exception.
    assert detect_key_role("eyJhbGci.@@@notbase64@@@.sig") is None
