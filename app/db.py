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
import logging
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from app.config import settings
from app.supabase_key import detect_key_role, is_privileged_key

log = logging.getLogger("db")

# Tables we persist in the local backend (mirrors the schema).
# (table, representative column, migration file) probes for columns/tables added
# by incremental migrations. schema_check() uses these to detect an un-applied
# migration before it 500s a live request.
_SCHEMA_PROBES = [
    ("tickets", "source_filename", "db/08_add_source_filename.sql"),
    ("reference_gtin", "gtin_14", "db/09_reference_masters.sql"),
    ("reference_part_info", "part_number", "db/09_reference_masters.sql"),
    ("reference_surgeons", "surgeon_distcode", "db/09_reference_masters.sql"),
    ("masters_ingests", "id", "db/09_reference_masters.sql"),
]

_LOCAL_TABLES = [
    "app_settings",
    "reference_lots",
    "reference_parts",
    "reference_gtin",
    "reference_part_info",
    "reference_surgeons",
    "log_ingests",
    "masters_ingests",
    "learning_part_desc",
    "learning_rep_map",
    "learning_price",
    "learning_gtin_xref",
    "learning_surgeon_map",
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

    def insert_many(self, table: str, new_rows: list[dict]) -> list[dict]:
        if not new_rows:
            return []
        with self._lock:
            rows = self._read(table)
            rows.extend(new_rows)
            self._write(table, rows)
            return list(new_rows)

    def replace_all(self, table: str, rows: list[dict], key_col: str = "id") -> None:
        with self._lock:
            self._write(table, list(rows))

    def delete_where(self, table: str, column: str, value: Any) -> int:
        with self._lock:
            rows = self._read(table)
            kept = [r for r in rows if r.get(column) != value]
            self._write(table, kept)
            return len(rows) - len(kept)

    def select(self, table: str) -> list[dict]:
        with self._lock:
            return self._read(table)

    def table_stats(self, table: str, stamp_col: str = "ingested_at") -> dict:
        rows = self.select(table)
        stamps = [r.get(stamp_col) for r in rows if r.get(stamp_col)]
        return {"rows": len(rows), "updated_at": max(stamps) if stamps else None}

    def find_all(self, table: str, column: str, value: Any) -> list[dict]:
        with self._lock:
            return [r for r in self._read(table) if r.get(column) == value]

    def find_one(self, table: str, column: str, value: Any) -> dict | None:
        with self._lock:
            for r in self._read(table):
                if r.get(column) == value:
                    return r
        return None

    def find_one_ci(self, table: str, column: str, value: str) -> dict | None:
        target = (value or "").strip().upper()
        with self._lock:
            for r in self._read(table):
                if (r.get(column) or "").strip().upper() == target:
                    return r
        return None

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

        # Warn loudly (but don't crash) if the configured key can't bypass RLS —
        # otherwise every write fails with a cryptic 42501 at runtime.
        if not is_privileged_key(settings.supabase_service_key):
            role = detect_key_role(settings.supabase_service_key) or "unknown"
            log.warning(
                "SUPABASE_SERVICE_KEY does not look like a service_role key "
                "(detected role=%s). Database writes will fail with row-level "
                "security errors until this is the service_role secret.",
                role,
            )
        self.client = create_client(settings.supabase_url, settings.supabase_service_key)

    def insert(self, table: str, row: dict) -> dict:
        res = self.client.table(table).insert(row).execute()
        return (res.data or [row])[0]

    def insert_many(self, table: str, rows: list[dict]) -> list[dict]:
        # One round-trip per chunk instead of one per row. PostgREST accepts a
        # JSON array; chunk to stay well under request-size limits.
        if not rows:
            return []
        out: list[dict] = []
        for i in range(0, len(rows), 500):
            res = self.client.table(table).insert(rows[i:i + 500]).execute()
            out.extend(res.data or [])
        return out

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

    def table_stats(self, table: str, stamp_col: str = "ingested_at") -> dict:
        count_res = self.client.table(table).select("*", count="exact").limit(0).execute()
        n = count_res.count or 0
        stamp_res = (self.client.table(table).select(stamp_col)
                     .order(stamp_col, desc=True).limit(1).execute())
        stamp = (stamp_res.data or [{}])[0].get(stamp_col)
        return {"rows": n, "updated_at": stamp}

    def delete_where(self, table: str, column: str, value: Any) -> int:
        res = self.client.table(table).delete().eq(column, value).execute()
        return len(res.data or [])

    def find_all(self, table: str, column: str, value: Any) -> list[dict]:
        # Predicate pushed to Postgres — never the 1000-row select() cap, and it
        # uses the table index instead of downloading rows to scan in Python.
        return self.client.table(table).select("*").eq(column, value).execute().data or []

    def find_one(self, table: str, column: str, value: Any) -> dict | None:
        rows = (self.client.table(table).select("*").eq(column, value)
                .limit(1).execute().data)
        return rows[0] if rows else None

    def find_one_ci(self, table: str, column: str, value: str) -> dict | None:
        # Case-insensitive exact match. Escape LIKE metacharacters (\ % _) so a
        # REF/lot code containing them matches literally, then ilike with no
        # wildcards = a case-insensitive equality test.
        pat = (value or "").strip().translate({0x5c: "\\\\", 0x25: "\\%", 0x5f: "\\_"})
        rows = (self.client.table(table).select("*").ilike(column, pat)
                .limit(1).execute().data)
        return rows[0] if rows else None

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

    def latest_log_ingest(self) -> dict | None:
        """Most recent Expiry Log ingest (for the 'last updated' indicator)."""
        rows = self.backend.select("log_ingests")
        if not rows:
            return None
        return max(rows, key=lambda r: r.get("ingested_at") or "")

    def reference_lots(self) -> list[dict]:
        return self.backend.select("reference_lots")

    def reference_parts(self) -> list[dict]:
        return self.backend.select("reference_parts")

    # ---- reference masters (GTIN / part_info / surgeon crosswalks) ----
    def replace_reference_gtin(self, rows: list[dict]) -> None:
        for r in rows:
            r.setdefault("ingested_at", _now_iso())
        self.backend.replace_all("reference_gtin", rows, key_col="gtin_14")

    def replace_reference_part_info(self, rows: list[dict]) -> None:
        for r in rows:
            r.setdefault("ingested_at", _now_iso())
        self.backend.replace_all("reference_part_info", rows, key_col="part_number")

    def replace_reference_surgeons(self, rows: list[dict]) -> None:
        for r in rows:
            r.setdefault("ingested_at", _now_iso())
        self.backend.replace_all("reference_surgeons", rows, key_col="surgeon_distcode")

    def log_masters_ingest(self, row: dict) -> dict:
        row.setdefault("id", new_id())
        row.setdefault("ingested_at", _now_iso())
        return self.backend.insert("masters_ingests", row)

    def latest_masters_ingest(self) -> dict | None:
        rows = self.backend.select("masters_ingests")
        if not rows:
            return None
        return max(rows, key=lambda r: r.get("ingested_at") or "")

    def has_masters(self) -> bool:
        """True once the product/surgeon masters have been loaded at least once."""
        return bool(self.backend.select("reference_gtin"))

    def schema_check(self) -> list[dict]:
        """Probe columns/tables added by incremental migrations so schema drift
        (a migration in db/ that was never applied) is caught at startup and via
        /diag instead of surfacing as a 500 on the first upload.

        Returns a list of problems (empty == schema is current). The offline
        JSON store has every column implicitly, so it always passes.
        """
        if self.offline:
            return []
        problems: list[dict] = []
        for table, column, migration in _SCHEMA_PROBES:
            try:
                self.backend.client.table(table).select(column).limit(1).execute()
            except Exception as exc:  # missing table/column -> PostgREST raises
                problems.append({"table": table, "column": column,
                                 "migration": migration, "error": str(exc)})
        return problems

    def masters_freshness(self) -> dict:
        """Actual current state of each masters table (row count + newest
        ingested_at). Uses table_stats() so Supabase returns the real count
        via the count=exact hint rather than the 1000-row select default."""
        return {
            "gtin":      self.backend.table_stats("reference_gtin"),
            "part_info": self.backend.table_stats("reference_part_info"),
            "surgeon":   self.backend.table_stats("reference_surgeons"),
        }

    def sku_for_gtin(self, gtin: str) -> dict | None:
        """Decoded (01) GTIN-14 -> product master row (SKU = Ref Number).

        Pushes the match to the DB so it works against the full master (the
        Supabase select() cap of 1000 rows would otherwise miss most GTINs).
        """
        if not gtin:
            return None
        return self.backend.find_one("reference_gtin", "gtin_14", str(gtin).strip())

    def part_info_for_ref(self, ref: str) -> dict | None:
        """Ref Number -> {description, part_type, category}. Exact match, then
        case-insensitive (trailing +/- are significant and preserved). Falls back
        to the built-in partner overlay (e.g. uniko), which survives the monthly
        full-replace of reference_part_info."""
        if not ref:
            return None
        from app import partner_parts

        return (self.backend.find_one("reference_part_info", "part_number", ref)
                or self.backend.find_one_ci("reference_part_info", "part_number", ref)
                or partner_parts.lookup(ref))

    def surgeon_for_key(self, key: str) -> dict | None:
        """<SurgeonLastName><DistCode> (normalized) -> surgeon_info record.
        Prefers an Active record when duplicates exist."""
        if not key:
            return None
        k = str(key).strip().upper()
        matches = self.backend.find_all("reference_surgeons", "surgeon_distcode", k)
        if not matches:  # keys are normalized upper at ingest; CI guards drift
            m = self.backend.find_one_ci("reference_surgeons", "surgeon_distcode", k)
            matches = [m] if m else []
        if not matches:
            return None
        for r in matches:
            if (r.get("status") or "").strip().lower() == "active":
                return r
        return matches[0]

    # ---- part_resolved view: learned override, else log ----
    def resolve_part_desc(self, ref: str) -> dict | None:
        if not ref:
            return None
        learned = self.backend.find_one_ci("learning_part_desc", "part_no", ref)
        if learned:
            return {**learned, "from_correction": True}
        p = self.backend.find_one_ci("reference_parts", "part_no", ref)
        if p:
            return {**p, "from_correction": False}
        return None

    def lot_lookup(self, lot: str) -> dict | None:
        if not lot:
            return None
        return (self.backend.find_one("reference_lots", "lot", lot.strip())
                or self.backend.find_one_ci("reference_lots", "lot", lot))

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

    def learn_surgeon_map(self, key: str, surgeon_full_name: str | None,
                          hospital: str | None, dist_code: str | None) -> None:
        """Learned surgeon chain from corrections: <SurgeonLastName><DistCode>
        -> surgeon/hospital/dist code. Additive upsert, mirrors the reference
        surgeons master shape (fallback only — the master always wins)."""
        self.backend.upsert(
            "learning_surgeon_map",
            ["surgeon_key"],
            {
                "surgeon_key": key,
                "surgeon_full_name": surgeon_full_name,
                "hospital": hospital,
                "dist_code": dist_code,
                "updated_at": _now_iso(),
            },
        )

    def learn_gtin_xref(self, gtin: str, part_no: str) -> None:
        existing = self.backend.find_one("learning_gtin_xref", "gtin", gtin)
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
        for r in self.backend.find_all("learning_price", "part_no", part_no):
            if r.get("hospital") == hospital:
                return float(r["unit_price"])
        return None

    def rep_for_code(self, rep_code: str) -> str | None:
        r = self.backend.find_one("learning_rep_map", "rep_code", rep_code)
        return r.get("rep_name") if r else None

    def ref_for_gtin(self, gtin: str) -> str | None:
        r = self.backend.find_one("learning_gtin_xref", "gtin", gtin)
        return r.get("part_no") if r else None

    def learned_surgeon_for_key(self, key: str) -> dict | None:
        if not key:
            return None
        return (self.backend.find_one("learning_surgeon_map", "surgeon_key", key)
                or self.backend.find_one_ci("learning_surgeon_map", "surgeon_key", key))

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
        return self.backend.find_one("batches", "id", batch_id)

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
        return self.backend.find_one("tickets", "ticket_id", ticket_id)

    def tickets_for_batch(self, batch_id: str) -> list[dict]:
        # Predicate pushed to the DB — never the 1000-row select() cap.
        return self.backend.find_all("tickets", "batch_id", batch_id)

    def pending_tickets(self, batch_id: str | None = None) -> list[dict]:
        rows = self.backend.find_all("tickets", "status", "pending_review")
        if batch_id:
            rows = [r for r in rows if r.get("batch_id") == batch_id]
        return rows

    # ---- line items ----
    def create_line_item(self, row: dict) -> dict:
        row.setdefault("line_id", new_id())
        row.setdefault("created_at", _now_iso())
        row.setdefault("flags", [])
        return self.backend.insert("line_items", row)

    def create_line_items(self, rows: list[dict]) -> list[dict]:
        """Bulk-create line items in one round-trip (defaults applied per row)."""
        for r in rows:
            r.setdefault("line_id", new_id())
            r.setdefault("created_at", _now_iso())
            r.setdefault("flags", [])
        if rows:
            self.backend.insert_many("line_items", rows)
        return rows

    def lines_for_ticket(self, ticket_id: str) -> list[dict]:
        return self.backend.find_all("line_items", "ticket_id", ticket_id)

    def clear_ticket_extractions(self, ticket_id: str) -> None:
        """Remove a ticket's line items + field snapshots so re-processing is
        idempotent (re-running Extract must replace, not append/duplicate)."""
        self.backend.delete_where("line_items", "ticket_id", ticket_id)
        self.backend.delete_where("field_extractions", "ticket_id", ticket_id)

    def delete_tickets_for_batch(self, batch_id: str) -> int:
        return self.backend.delete_where("tickets", "batch_id", batch_id)

    def delete_batch(self, batch_id: str) -> int:
        return self.backend.delete_where("batches", "id", batch_id)

    # ---- field extractions (per-field snapshot) ----
    def add_field_extraction(self, row: dict) -> None:
        row.setdefault("created_at", _now_iso())
        self.backend.insert("field_extractions", row)

    def add_field_extractions(self, rows: list[dict]) -> None:
        """Bulk-insert per-field snapshots in one round-trip."""
        for r in rows:
            r.setdefault("created_at", _now_iso())
        if rows:
            self.backend.insert_many("field_extractions", rows)

    def field_extractions_for_ticket(self, ticket_id: str) -> list[dict]:
        # The highest-volume table (~10 rows per line item). MUST push the
        # ticket_id filter to the DB or the 1000-row select() cap silently drops
        # recent tickets' confidence + raw snapshots -> a blank deliverable.
        return self.backend.find_all("field_extractions", "ticket_id", ticket_id)

    # ---- corrections audit + uploads log ----
    def add_correction_audit(self, row: dict) -> None:
        row.setdefault("corrected_at", _now_iso())
        self.backend.insert("corrections_audit", row)

    def log_corrected_upload(self, row: dict) -> dict:
        row.setdefault("id", new_id())
        row.setdefault("uploaded_at", _now_iso())
        return self.backend.insert("corrected_uploads", row)

    def list_corrected_uploads(self, limit: int = 50) -> list[dict]:
        """Retraining uploads, newest first (for the History tab)."""
        rows = self.backend.select("corrected_uploads")
        rows.sort(key=lambda r: r.get("uploaded_at") or "", reverse=True)
        return rows[:limit]

    def list_learning_counts(self) -> dict[str, int]:
        """Cumulative row count of each learning store (uncapped count=exact).

        Pass each table's real stamp column so the Supabase order query in
        table_stats targets a column that exists.
        """
        specs = {
            "learning_price": "last_seen",
            "learning_part_desc": "updated_at",
            "learning_rep_map": "updated_at",
            "learning_gtin_xref": "updated_at",
            "learning_surgeon_map": "updated_at",
        }
        return {t: self.backend.table_stats(t, stamp_col=col).get("rows", 0)
                for t, col in specs.items()}

    def learning_prices(self) -> list[dict]:
        rows = self.backend.select("learning_price")
        return sorted(rows, key=lambda r: r.get("last_seen") or "", reverse=True)

    def learning_part_descs(self) -> list[dict]:
        rows = self.backend.select("learning_part_desc")
        return sorted(rows, key=lambda r: r.get("updated_at") or "", reverse=True)

    def learning_reps(self) -> list[dict]:
        rows = self.backend.select("learning_rep_map")
        return sorted(rows, key=lambda r: r.get("updated_at") or "", reverse=True)

    def learning_gtin_xrefs(self) -> list[dict]:
        rows = self.backend.select("learning_gtin_xref")
        return sorted(rows, key=lambda r: r.get("updated_at") or "", reverse=True)

    def learning_surgeon_maps(self) -> list[dict]:
        rows = self.backend.select("learning_surgeon_map")
        return sorted(rows, key=lambda r: r.get("updated_at") or "", reverse=True)

    # ---- app_settings (key/value) ----
    def get_app_setting(self, key: str) -> str | None:
        r = self.backend.find_one("app_settings", "key", key)
        return r.get("value") if r else None

    def set_app_setting(self, key: str, value: str) -> None:
        self.backend.upsert("app_settings", ["key"], {"key": key, "value": str(value)})

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
