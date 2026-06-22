"""FastAPI app + routes.

Implements the API contract from DEVELOPER_HANDOFF §5 and serves the friendly
web UI (app/static) at the root. All routes are server-side; in production
Traefik terminates TLS in front.
"""
from __future__ import annotations

import io
import logging
import traceback
from asyncio import get_event_loop
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, File, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db import db
from app.jobs import shutdown_scheduler, start_scheduler
from app.metrics import auto_resolve_by_week
from app.pipeline.run import ingest_image, run_batch
from app.storage import OUTPUT_SHEETS, get_object, split_ref
from app.supabase_key import WRONG_KEY_HELP, detect_key_role
from app.version import CHANGELOG, VERSION

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("main")

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm-start the product/surgeon masters from the repo-bundled CSVs if the
    # datastore has none yet. Operators re-upload fresh copies via /reference/masters.
    try:
        if not db.has_masters():
            from app.learning.ingest_reference import load_bundled_masters

            summary = load_bundled_masters()
            log.info("Seeded reference masters: %s", summary)
    except Exception as exc:  # pragma: no cover - never block startup on seeding
        log.warning("Reference masters seed skipped: %s", exc)

    # Start the scheduler only with a real datastore (skip in offline dev).
    if not db.offline:
        start_scheduler()
    else:
        log.info("OFFLINE_MODE: scheduler not started, using local JSON store")
    yield
    shutdown_scheduler()


_executor = ThreadPoolExecutor(max_workers=2)

app = FastAPI(title="Usage — Label Extraction", version=VERSION, lifespan=lifespan)


# ---------------------------------------------------------------------------
# Health + version
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/version")
def version():
    return {"version": VERSION, "changelog": CHANGELOG}


@app.get("/diag")
def diag():
    """Non-secret diagnostics. Reports the datastore mode and the *role* of the
    configured Supabase key (never the key itself) so a misconfigured
    service_role key can be spotted without pasting any secret."""
    info: dict = {"datastore": "offline" if db.offline else "supabase"}
    if not db.offline:
        role = detect_key_role(settings.supabase_service_key) or "unknown"
        info["key_role"] = role
        info["key_ok"] = role == "service_role"
    return info


def _explain_ingest_error(exc: Exception) -> str:
    """Turn raw datastore errors into operator-actionable messages."""
    msg = str(exc)
    if "42501" in msg or "row-level security" in msg:
        return f"Supabase rejected the write (row-level security). {WRONG_KEY_HELP}"
    return f"Could not process the Expiry Log: {exc}"


# ---------------------------------------------------------------------------
# Intake: upload ticket images (redacts, stores redacted, creates ticket rows)
# ---------------------------------------------------------------------------
@app.post("/images", status_code=202)
async def upload_images(files: list[UploadFile] = File(...)):
    batch = db.create_batch()
    results = []
    for f in files:
        data = await f.read()  # raw bytes held in memory only
        result = ingest_image(data, f.filename or "", batch["id"])
        results.append({"ticket_id": result["ticket_id"], "status": result["status"]})
    return {"batch_id": batch["id"], "tickets": results}


# ---------------------------------------------------------------------------
# Run a batch (process pending tickets -> colored workbook)
# ---------------------------------------------------------------------------
@app.post("/batches/run")
def batches_run(payload: dict | None = Body(default=None)):
    batch_id = (payload or {}).get("batch_id") if payload else None
    result = run_batch(batch_id)
    return result


@app.get("/batches")
def list_batches():
    out = []
    for b in db.list_batches():
        out.append({
            "batch_id": b["id"],
            "run_date": b.get("run_date"),
            "ticket_count": b.get("ticket_count") or 0,
            "status": b.get("status"),
        })
    return out


@app.get("/batches/{batch_id}/sheet")
def get_batch_sheet(batch_id: str):
    batch = db.get_batch(batch_id)
    if not batch or not batch.get("output_sheet_path"):
        return JSONResponse({"error": "sheet not found"}, status_code=404)
    bucket, path = split_ref(batch["output_sheet_path"])
    data = get_object(bucket, path)
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="review_{batch_id[:8]}.xlsx"'},
    )


# ---------------------------------------------------------------------------
# Corrections re-upload (multi-file, matched by ticket_id)
# ---------------------------------------------------------------------------
@app.post("/corrections/upload")
async def corrections_upload(files: list[UploadFile] = File(...)):
    from app.learning.diff import diff_ticket
    from app.learning.harvest import harvest_ticket
    from app.sheets.read import parse_corrected_workbook
    from app.storage import CORRECTED_UPLOADS, put_object

    processed = 0
    matched = 0
    unknown = 0
    for f in files:
        data = await f.read()
        path = put_object(CORRECTED_UPLOADS, f.filename or "corrected.xlsx", data,
                          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        try:
            parsed = parse_corrected_workbook(data)
        except Exception as e:
            log.warning("could not parse %s: %s", f.filename, e)
            db.log_corrected_upload({"file_path": path, "status": "error"})
            continue

        sheet_matched = 0
        sheet_unknown = 0
        for ticket_id, corrected in parsed["tickets"].items():
            ticket = db.get_ticket(ticket_id)
            if ticket is None:
                sheet_unknown += 1
                continue
            # A. Harvest facts (always works).
            harvest_ticket(corrected)
            # B. Diff for calibration (only if snapshot still present).
            diff_ticket(corrected)
            # C. Mark verified.
            db.update_ticket(ticket_id, {"status": "verified"})
            sheet_matched += 1

        processed += 1
        matched += sheet_matched
        unknown += sheet_unknown
        db.log_corrected_upload({
            "file_path": path,
            "sheets_processed": 1,
            "tickets_matched": sheet_matched,
            "tickets_unknown": sheet_unknown,
            "status": "processed",
        })

    return {"processed": processed, "tickets_matched": matched, "tickets_unknown": unknown}


# ---------------------------------------------------------------------------
# Reference log full-replace
# ---------------------------------------------------------------------------
@app.post("/reference/log")
async def reference_log(file: UploadFile = File(...)):
    from app.learning.ingest_log import ingest_expiry_log
    from app.storage import REFERENCE_LOGS, put_object

    data = await file.read()
    fname = file.filename or "Expiry_Log.xlsx"

    # Store a copy for audit (non-fatal: log and continue on failure).
    try:
        put_object(REFERENCE_LOGS, fname, data,
                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except Exception as exc:
        log.warning("Reference log storage upload failed (non-fatal): %s", exc)

    # Parse + full-replace reference tables. Run on a thread so we don't
    # block the asyncio event loop during the 60k-row Supabase insert.
    loop = get_event_loop()
    try:
        summary = await loop.run_in_executor(_executor, ingest_expiry_log, data)
    except Exception as exc:
        log.error("ingest_expiry_log failed: %s", traceback.format_exc())
        return JSONResponse({"detail": _explain_ingest_error(exc)}, status_code=500)
    return summary


@app.post("/reference/masters")
async def reference_masters(files: list[UploadFile] = File(...)):
    """Full-replace the product/surgeon masters. Routes each CSV by filename:
    GTIN_Codes.csv, part_info.csv, surgeon_info.csv (any subset accepted)."""
    from app.learning.ingest_reference import ingest_masters

    kwargs: dict = {}
    for f in files:
        name = (f.filename or "").lower()
        data = await f.read()
        if "gtin" in name:
            kwargs["gtin"] = data
        elif "part" in name:
            kwargs["part_info"] = data
        elif "surgeon" in name:
            kwargs["surgeon_info"] = data
    if not kwargs:
        return JSONResponse(
            {"detail": "No recognized masters file (expect GTIN_Codes.csv, "
                       "part_info.csv, surgeon_info.csv)."},
            status_code=400,
        )
    loop = get_event_loop()
    summary = await loop.run_in_executor(_executor, lambda: ingest_masters(**kwargs))
    return summary


@app.get("/reference/status")
def reference_status():
    """When the Expiry Log + masters were last updated (for the UI banner)."""
    latest = db.latest_log_ingest()
    masters = db.latest_masters_ingest()
    out: dict = {"loaded": bool(latest)}
    if latest:
        out.update({
            "updated_at": latest.get("ingested_at"),
            "row_count": latest.get("row_count"),
            "unique_parts": latest.get("unique_parts"),
            "unique_lots": latest.get("unique_lots"),
        })
    if masters:
        out["masters"] = {
            "updated_at": masters.get("ingested_at"),
            "gtin_rows": masters.get("gtin_rows"),
            "part_rows": masters.get("part_rows"),
            "surgeon_rows": masters.get("surgeon_rows"),
        }
    return out


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
@app.get("/metrics/auto-resolve")
def metrics_auto_resolve(weeks: int = 8):
    return auto_resolve_by_week(weeks)


# ---------------------------------------------------------------------------
# Static UI (mounted last so API routes win). html=True serves index.html at /.
# ---------------------------------------------------------------------------
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
