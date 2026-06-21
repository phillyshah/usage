"""Data-access layer.

One ``Database`` facade with two interchangeable backends:

* **Supabase** (production) — uses the service-role key, which bypasses RLS.
* **Local JSON** (OFFLINE_MODE) — mirrors every table as a JSON file on disk so
  the whole pipeline, the UI, and the bundled fixtures run with no live creds.

The rest of the codebase only ever touches the high-level helpers here; it never
imports the Supabase client directly. Table and column names match
``supabase_schema.sql`` exactly.
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from app.config import settings

# Tables we persist in the local backend (mirrors the schema).
_LOCAL_TABLES = [
    "app_settings",
    "reference_lots",
    "reference_parts",
    "log_ingests",
    "learning_part_desc",
    "learning_rep_map",
    "learning_price",
    "learning_gtin_xref",
    "batches",
    "tickets",
    "line_items",
    "field_extractions",
    "corrections_audit",
    "corrected_uploads",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Local JSON backend
# ---------------------------------------------------------------------------
class _LocalBackend:
    """Tiny JSON-file store. Not concurrent-safe across processes, but fine for
    single-process offline dev and CI. Guarded by a lock for thread safety."""

    def __init__(self, root: str):
        self.root = Path(root)
        self.dbdir = self.root / "db"
        self.dbdir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        for t in _LOCAL_TABLES:
            f = self.dbdir / f"{t}.json"
            if not f.exists():
                f.write_text("[]")
        # Seed app_settings.retention_days like the schema does.
        if not self._read("app_settings"):
            self._write(
                "app_settings",
                [{"key": "retention_days", "value": str(settings.retention_days)}],
            )

    def _path(self, table: str) -> Path:
        return self.dbdir / f"{table}.json"

    def _read(self, table: str) -> list[dict]:
        try:
            return json.loads(self._path(table).read_text())
        except FileNotFoundError:
            return []

    def _write(self, table: str, rows: list[dict]) -> None:
        self._path(table).write_text(json.dumps(rows, default=str, indent=2))

    # -- generic ops --
    def insert(self, table: str, row: dict) -> dict:
        with self._lock:
            rows = self._read(table)
            rows.append(row)
            self._write(table, rows)
            return row

    def replace_all(self, table: str, rows: list[dict], key_col: str = "id") -> None:
        with self._lock:
            self._write(table, list(rows))

    def select(self, table: str) -> list[dict]:
        with self._lock:
            return self._read(table)

    def update_where(self, table: str, key: str, value: Any, patch: dict) -> int:
        with self._lock:
            rows = self._read(table)
            n = 0
            for r in rows:
                if r.get(key) == value:
                    r.update(patch)
                    n += 1
            self._write(table, rows)
            return n

    def upsert(self, table: str, pk: Iterable[str], row: dict) -> None:
        pk = list(pk)
        with self._lock:
            rows = self._read(table)
            for r in rows:
                if all(r.get(k) == row.get(k) for k in pk):
                    r.update(row)
                    self._write(table, rows)
                    return
            rows.append(row)
            self._write(table, rows)


# ---------------------------------------------------------------------------
# Supabase backend
# ---------------------------------------------------------------------------
class _SupabaseBackend:
    def __init__(self):
        from supabase import create_client  # imported lazily

        self.client = create_client(settings.supabase_url, settings.supabase_service_key)

    def insert(self, table: str, row: dict) -> dict:
        res = self.client.table(table).insert(row).execute()
        return (res.data or [row])[0]

    def replace_all(self, table: str, rows: list[dict], key_col: str = "id") -> None:
        # Full replace: delete every row, then insert in chunks. PostgREST
        # requires a filter on delete; "column is not null" matches all rows and
        # works whether the pk is a serial id (reference_lots) or text
        # (reference_parts.part_no). Pass Python None so supabase-py 2.x
        # generates the correct "not.is.null" PostgREST filter.
        self.client.table(table).delete().not_.is_(key_col, None).execute()
        for i in range(0, len(rows), 1000):
            self.client.table(table).insert(rows[i : i + 1000]).execute()

    def select(self, table: str) -> list[dict]:
        return self.client.table(table).select("*").execute().data or []

    def update_where(self, table: str, key: str, value: Any, patch: dict) -> int:
        res = self.client.table(table).update(patch).eq(key, value).execute()
        return len(res.data or [])

    def upsert(self, table: str, pk: Iterable[str], row: dict) -> None:
        self.client.table(table).upsert(row).execute()


# ---------------------------------------------------------------------------
# Database facade — the only thing the rest of the app imports
# ---------------------------------------------------------------------------
class Database:
    def __init__(self):
        if settings.has_supabase:
            self.backend: Any = _SupabaseBackend()
            self.offline = False
        else:
            self.backend = _LocalBackend(settings.local_data_dir)
            self.offline = True

    # ---- reference data (full replace on each Expiry Log ingest) ----
    def replace_reference(self, lots: list[dict], parts: list[dict]) -> None:
        for i, r in enumerate(lots, start=1):
            # Only the local backend needs a synthetic id; Supabase uses its
            # bigserial so the sequence stays consistent.
            if self.offline:
                r.setdefault("id", i)
            r.setdefault("ingested_at", _now_iso())
        for p in parts:
            p.setdefault("last_seen", _now_iso())
        # reference_lots keys on the serial id; reference_parts keys on part_no.
        self.backend.replace_all("reference_lots", lots, key_col="id")
        self.backend.replace_all("reference_parts", parts, key_col="part_no")

    def log_ingest(self, row: dict) -> dict:
        row.setdefault("id", new_id())
        row.setdefault("ingested_at", _now_iso())
        return self.backend.insert("log_ingests", row)

    def reference_lots(self) -> list[dict]:
        return self.backend.select("reference_lots")

    def reference_parts(self) -> list[dict]:
        return self.backend.select("reference_parts")

    # ---- part_resolved view: learned override, else log ----
    def resolve_part_desc(self, ref: str) -> dict | None:
        if not ref:
            return None
        key = ref.strip().upper()
        for r in self.backend.select("learning_part_desc"):
            if (r.get("part_no") or "").strip().upper() == key:
                return {**r, "from_correction": True}
        for r in self.backend.select("reference_parts"):
            if (r.get("part_no") or "").strip().upper() == key:
                return {**r, "from_correction": False}
        return None

    def lot_lookup(self, lot: str) -> dict | None:
        if not lot:
            return None
        key = lot.strip().upper()
        for r in self.backend.select("reference_lots"):
            if (r.get("lot") or "").strip().upper() == key:
                return r
        return None

    def ref_in_log(self, ref: str) -> bool:
        return self.resolve_part_desc(ref) is not None

    # ---- learning stores ----
    def learn_part_desc(self, part_no: str, description: str | None, size: str | None) -> None:
        self.backend.upsert(
            "learning_part_desc",
            ["part_no"],
            {
                "part_no": part_no,
                "description": description,
                "size": size,
                "updated_at": _now_iso(),
            },
        )

    def learn_rep(self, rep_code: str, rep_name: str) -> None:
        self.backend.upsert(
            "learning_rep_map",
            ["rep_code"],
            {"rep_code": rep_code, "rep_name": rep_name, "updated_at": _now_iso()},
        )

    def learn_price(self, part_no: str, hospital: str, unit_price: float) -> None:
        self.backend.upsert(
            "learning_price",
            ["part_no", "hospital"],
            {
                "part_no": part_no,
                "hospital": hospital,
                "unit_price": unit_price,
                "last_seen": _now_iso(),
            },
        )

    def learn_gtin_xref(self, gtin: str, part_no: str) -> None:
        existing = None
        for r in self.backend.select("learning_gtin_xref"):
            if r.get("gtin") == gtin:
                existing = r
                break
        confirmations = (existing.get("confirmations", 0) + 1) if existing else 1
        self.backend.upsert(
            "learning_gtin_xref",
            ["gtin"],
            {
                "gtin": gtin,
                "part_no": part_no,
                "confirmations": confirmations,
                "updated_at": _now_iso(),
            },
        )

    def price_suggestion(self, part_no: str, hospital: str) -> float | None:
        if not (part_no and hospital):
            return None
        for r in self.backend.select("learning_price"):
            if r.get("part_no") == part_no and r.get("hospital") == hospital:
                return float(r["unit_price"])
        return None

    def rep_for_code(self, rep_code: str) -> str | None:
        for r in self.backend.select("learning_rep_map"):
            if r.get("rep_code") == rep_code:
                return r.get("rep_name")
        return None

    def ref_for_gtin(self, gtin: str) -> str | None:
        for r in self.backend.select("learning_gtin_xref"):
            if r.get("gtin") == gtin:
                return r.get("part_no")
        return None

    # ---- batches ----
    def create_batch(self) -> dict:
        row = {
            "id": new_id(),
            "run_date": date.today().isoformat(),
            "created_at": _now_iso(),
            "output_sheet_path": None,
            "ticket_count": 0,
            "status": "generated",
        }
        return self.backend.insert("batches", row)

    def update_batch(self, batch_id: str, patch: dict) -> None:
        self.backend.update_where("batches", "id", batch_id, patch)

    def list_batches(self) -> list[dict]:
        rows = self.backend.select("batches")
        rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return rows

    def get_batch(self, batch_id: str) -> dict | None:
        for r in self.backend.select("batches"):
            if r.get("id") == batch_id:
                return r
        return None

    # ---- tickets ----
    def create_ticket(self, row: dict) -> dict:
        row.setdefault("ticket_id", new_id())
        row.setdefault("created_at", _now_iso())
        # Mirror the DB trigger: expires_at = created_at + retention_days.
        ret = self._retention_days()
        created = datetime.fromisoformat(row["created_at"])
        row.setdefault("expires_at", (created + timedelta(days=ret)).isoformat())
        row.setdefault("flags", [])
        row.setdefault("status", "pending_review")
        return self.backend.insert("tickets", row)

    def update_ticket(self, ticket_id: str, patch: dict) -> None:
        self.backend.update_where("tickets", "ticket_id", ticket_id, patch)

    def get_ticket(self, ticket_id: str) -> dict | None:
        for r in self.backend.select("tickets"):
            if r.get("ticket_id") == ticket_id:
                return r
        return None

    def tickets_for_batch(self, batch_id: str) -> list[dict]:
        return [r for r in self.backend.select("tickets") if r.get("batch_id") == batch_id]

    def pending_tickets(self, batch_id: str | None = None) -> list[dict]:
        rows = [r for r in self.backend.select("tickets") if r.get("status") == "pending_review"]
        if batch_id:
            rows = [r for r in rows if r.get("batch_id") == batch_id]
        return rows

    # ---- line items ----
    def create_line_item(self, row: dict) -> dict:
        row.setdefault("line_id", new_id())
        row.setdefault("created_at", _now_iso())
        row.setdefault("flags", [])
        return self.backend.insert("line_items", row)

    def lines_for_ticket(self, ticket_id: str) -> list[dict]:
        return [r for r in self.backend.select("line_items") if r.get("ticket_id") == ticket_id]

    # ---- field extractions (per-field snapshot) ----
    def add_field_extraction(self, row: dict) -> None:
        row.setdefault("created_at", _now_iso())
        self.backend.insert("field_extractions", row)

    def field_extractions_for_ticket(self, ticket_id: str) -> list[dict]:
        return [
            r for r in self.backend.select("field_extractions") if r.get("ticket_id") == ticket_id
        ]

    # ---- corrections audit + uploads log ----
    def add_correction_audit(self, row: dict) -> None:
        row.setdefault("corrected_at", _now_iso())
        self.backend.insert("corrections_audit", row)

    def log_corrected_upload(self, row: dict) -> dict:
        row.setdefault("id", new_id())
        row.setdefault("uploaded_at", _now_iso())
        return self.backend.insert("corrected_uploads", row)

    # ---- metrics ----
    def corrections_audit(self) -> list[dict]:
        return self.backend.select("corrections_audit")

    # ---- retention ----
    def _retention_days(self) -> int:
        for r in self.backend.select("app_settings"):
            if r.get("key") == "retention_days":
                try:
                    return int(r["value"])
                except (TypeError, ValueError):
                    pass
        return settings.retention_days

    def expired_tickets(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        out = []
        for r in self.backend.select("tickets"):
            exp = r.get("expires_at")
            if exp and datetime.fromisoformat(exp) < now:
                out.append(r)
        return out

    def purge_expired_field_extractions(self) -> int:
        if not self.offline:
            # Supabase: use the SQL function for an atomic server-side purge.
            self.backend.client.rpc("purge_expired_extractions").execute()
            return -1
        expired_ids = {t["ticket_id"] for t in self.expired_tickets()}
        rows = self.backend.select("field_extractions")
        kept = [r for r in rows if r.get("ticket_id") not in expired_ids]
        removed = len(rows) - len(kept)
        self.backend.replace_all("field_extractions", kept)
        return removed


# Module-level singleton.
db = Database()
