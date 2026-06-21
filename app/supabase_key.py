"""Supabase API-key inspection.

The app must talk to Supabase with the **service_role** key, which bypasses
row-level security (RLS). Every table has RLS enabled with no policies, so a
publishable / anon key gets `42501 new row violates row-level security policy`
on the very first write.

These helpers decode the configured key's *role* — without verifying or trusting
its signature and without ever logging the key itself — so the app can warn the
operator early and translate the cryptic 42501 into an actionable message.

Supported key shapes:
* Legacy JWTs (`eyJ...`) carry a ``role`` claim: ``service_role`` or ``anon``.
* New-style keys are prefixed: ``sb_secret_...`` (privileged) /
  ``sb_publishable_...`` (not privileged).
"""
from __future__ import annotations

import base64
import binascii
import json


def detect_key_role(key: str | None) -> str | None:
    """Return 'service_role' | 'anon' | None (unknown) for a Supabase API key.

    Never raises; returns None when the shape is unrecognised.
    """
    if not key:
        return None

    # New-style prefixed keys.
    if key.startswith("sb_secret_"):
        return "service_role"
    if key.startswith("sb_publishable_"):
        return "anon"

    # Legacy JWT: header.payload.signature — read the role claim from payload.
    parts = key.split(".")
    if len(parts) == 3:
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)  # restore base64url padding
        try:
            data = json.loads(base64.urlsafe_b64decode(payload))
        except (binascii.Error, ValueError, json.JSONDecodeError):
            return None
        role = data.get("role")
        return role if isinstance(role, str) else None

    return None


def is_privileged_key(key: str | None) -> bool:
    """True only when the key is the service_role secret (bypasses RLS)."""
    return detect_key_role(key) == "service_role"


# Operator-facing guidance reused by the startup warning and the error response.
WRONG_KEY_HELP = (
    "The SUPABASE_SERVICE_KEY in your .env is not the service_role secret "
    "(it looks like the publishable / anon key, which cannot bypass row-level "
    "security). Copy the service_role key from Supabase → Project Settings "
    "→ API → Project API keys, put it in .env as SUPABASE_SERVICE_KEY, "
    "then restart the container."
)
