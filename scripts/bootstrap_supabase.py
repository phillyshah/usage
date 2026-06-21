#!/usr/bin/env python3
"""One-shot Supabase bootstrap: create the four private Storage buckets and
verify the schema tables exist.

The SQL file (supabase_schema.sql) creates the tables, view, trigger, and the
pg_cron purge job — but it intentionally does NOT create Storage buckets (object
storage isn't managed from SQL). This script closes that gap. It is idempotent:
re-running it is safe.

Usage:
    # with .env populated (SUPABASE_URL, SUPABASE_SERVICE_KEY, OFFLINE_MODE=false)
    python scripts/bootstrap_supabase.py

Run this AFTER applying supabase_schema.sql in the SQL editor.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running as `python scripts/bootstrap_supabase.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings

BUCKETS = [
    "redacted-images",
    "output-sheets",
    "corrected-uploads",
    "reference-logs",
]

REQUIRED_TABLES = [
    "app_settings", "reference_lots", "reference_parts", "log_ingests",
    "learning_part_desc", "learning_rep_map", "learning_price", "learning_gtin_xref",
    "batches", "tickets", "line_items", "field_extractions",
    "corrections_audit", "corrected_uploads",
]


def main() -> int:
    if settings.offline_mode or not (settings.supabase_url and settings.supabase_service_key):
        print("ERROR: Supabase is not configured. Set SUPABASE_URL + "
              "SUPABASE_SERVICE_KEY and OFFLINE_MODE=false in .env.", file=sys.stderr)
        return 2

    from supabase import create_client

    client = create_client(settings.supabase_url, settings.supabase_service_key)

    # --- 1. Storage buckets (all private) ---
    try:
        existing = {b.name for b in client.storage.list_buckets()}
    except Exception as e:
        print(f"ERROR: could not list buckets: {e}", file=sys.stderr)
        return 1

    for name in BUCKETS:
        if name in existing:
            print(f"  bucket '{name}' already exists — ok")
            continue
        try:
            client.storage.create_bucket(name, options={"public": False})
            print(f"  created private bucket '{name}'")
        except Exception as e:
            print(f"  WARNING: could not create bucket '{name}': {e}", file=sys.stderr)

    # --- 2. Verify schema tables are present ---
    missing = []
    for t in REQUIRED_TABLES:
        try:
            client.table(t).select("*").limit(1).execute()
        except Exception:
            missing.append(t)
    if missing:
        print("\nERROR: these tables are missing — apply supabase_schema.sql first:",
              file=sys.stderr)
        for t in missing:
            print(f"  - {t}", file=sys.stderr)
        return 1

    print("\nAll buckets present and all schema tables reachable. Bootstrap complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
