"""Orchestration: ingest one image (redact gate) and run a batch.

Ingest (`ingest_image`) is the PHI gate path used by POST /images:
    raw bytes (in memory) -> preprocess -> detect template -> REDACT
      -> if not located: ticket=manual_queue, store NOTHING, return
      -> else: store ONLY the redacted image, ticket=pending_review

Batch processing (`run_batch`) is used by POST /batches/run and the scheduler:
    for each pending ticket -> load redacted image -> decode barcodes
      -> resolve refs -> vision fallback -> score + persist -> write workbook
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from app.db import db
from app.pipeline import assemble, barcode, preprocess, vision
from app.pipeline.redact import redact_patient_region
from app.pipeline.template import detect_template, geometry_for
from app.storage import REDACTED_IMAGES, get_object, put_object, split_ref

log = logging.getLogger("pipeline.run")


def ingest_image(data: bytes, filename: str, batch_id: str) -> dict:
    """Redact + store one ticket image. Returns {ticket_id, status}.

    Raw bytes live only in memory here; only the redacted image is ever stored.
    """
    img = preprocess.decode_image(data)
    # No heavy enhancement here: redaction needs only the template geometry, and
    # storing the *original* (minus the patient box) keeps the DataMatrix and
    # printed text crisp for extraction — denoising both costs seconds per upload
    # and degrades barcode decoding. (FIELD_GUIDE §9 still holds: we store only
    # the redacted image, never the raw one.)
    template = detect_template(img, filename)
    redacted, located = redact_patient_region(img, template)

    if not located:
        # Fail safe: cannot prove PHI is masked -> manual queue, store nothing.
        ticket = db.create_ticket({
            "batch_id": batch_id,
            "source_image_path": None,
            "source_filename": filename or None,
            "entity": template if template != "Unknown" else None,
            "status": "manual_queue",
            "flags": ["Patient region could not be located — manual review required"],
        })
        log.info("ticket %s routed to manual_queue (%s)", ticket["ticket_id"], filename)
        return {"ticket_id": ticket["ticket_id"], "status": "manual_queue"}

    # Encode the redacted image BEFORE persisting anything. If encoding fails we
    # cannot prove the stored bytes are masked, so we must never fall back to the
    # raw upload (that would leak PHI). Fail safe to manual_queue, store nothing.
    redacted_bytes = preprocess.encode_image(redacted, ".jpg")
    if not redacted_bytes:
        ticket = db.create_ticket({
            "batch_id": batch_id,
            "source_image_path": None,
            "source_filename": filename or None,
            "entity": template,
            "status": "manual_queue",
            "flags": ["Could not encode a redacted image — manual review required"],
        })
        log.info("ticket %s routed to manual_queue (encode failed, %s)",
                 ticket["ticket_id"], filename)
        return {"ticket_id": ticket["ticket_id"], "status": "manual_queue"}

    # Store ONLY the redacted image. If the store fails, flip the ticket to
    # manual_queue (rather than leave a pending ticket pointing at nothing) and
    # let the caller report the failure.
    ticket = db.create_ticket({
        "batch_id": batch_id,
        "entity": template,
        "source_filename": filename or None,
        "status": "pending_review",
    })
    try:
        ref = put_object(REDACTED_IMAGES, f"{ticket['ticket_id']}.jpg",
                         redacted_bytes, "image/jpeg")
    except Exception:
        db.update_ticket(ticket["ticket_id"], {
            "status": "manual_queue",
            "flags": ["Could not store the redacted image — manual review required"],
        })
        raise
    db.update_ticket(ticket["ticket_id"], {"source_image_path": ref})
    return {"ticket_id": ticket["ticket_id"], "status": "pending_review"}


def _grid_crop(img, template: str):
    """Crop the label-grid region for a template; whole image if unknown geom."""
    if img is None:
        return None
    geom = geometry_for(template)
    if geom is None:
        return img
    h, w = img.shape[:2]
    x, y, gw, gh = geom.grid_region.to_pixels(w, h)
    return img[max(0, y): y + gh, max(0, x): x + gw]


def process_ticket(ticket: dict) -> dict:
    """Run extraction for a single pending ticket and persist the result."""
    ticket_id = ticket["ticket_id"]
    img = None
    redacted_bytes = b""
    ref = ticket.get("source_image_path")
    if ref:
        try:
            bucket, path = split_ref(ref)
            redacted_bytes = get_object(bucket, path)
            img = preprocess.decode_image(redacted_bytes)
        except Exception as e:  # pragma: no cover
            log.warning("could not load redacted image for %s: %s", ticket_id, e)

    template = ticket.get("entity") or "Maxx Orthopedics"

    # Deterministic first: decode device labels from the grid region.
    grid = _grid_crop(img, template)
    labels = barcode.decode_region(grid) if grid is not None else []

    # Vision fallback: header, prices, qty, totals (single call on redacted bytes).
    vresult = vision.extract_handwritten(redacted_bytes) if redacted_bytes else vision.extract_handwritten(b"")

    # If vision returned more priced lines than decoded labels, pad with empty
    # label dicts so vision-only lines still appear (barcode failed on those).
    vlines = vresult.get("lines", []) if vresult else []
    while len(labels) < len(vlines):
        labels.append({"gtin": None, "lot": None, "expiry": None, "mfg": None, "serial": None, "raw": None, "decoded": False, "ref": None})

    summary = assemble.assemble_and_persist(ticket, vresult, labels)
    return summary


def run_batch(batch_id: str | None = None) -> dict:
    """Process all pending tickets (optionally just one batch) and write the sheet."""
    from app.sheets.write import write_review_workbook
    from app.storage import OUTPUT_SHEETS

    pending = db.pending_tickets(batch_id)
    if not pending and batch_id:
        # Re-run on an already-processed batch: include its tickets for the sheet.
        pending = []

    # Process tickets concurrently: each ticket's work is barcode decode (native,
    # releases the GIL), one vision API call (network), and bulk DB writes
    # (network) — all I/O-bound, so threads overlap the latency. Capped to keep
    # the vision API within sane concurrency.
    def _safe_process(ticket: dict) -> None:
        try:
            process_ticket(ticket)
        except Exception as e:  # pragma: no cover
            log.exception("failed to process ticket %s: %s", ticket.get("ticket_id"), e)
            db.update_ticket(ticket["ticket_id"], {"flags": [f"Processing error: {e}"]})

    if pending:
        with ThreadPoolExecutor(max_workers=min(6, len(pending))) as ex:
            list(ex.map(_safe_process, pending))

    # Determine the batch to render.
    if batch_id is None:
        batch = db.create_batch()
        batch_id = batch["id"]
        # attach freshly-processed tickets that have no batch to this batch
        for t in pending:
            if not t.get("batch_id"):
                db.update_ticket(t["ticket_id"], {"batch_id": batch_id})

    tickets = db.tickets_for_batch(batch_id)
    # Build the workbook from persisted rows.
    workbook_bytes = write_review_workbook(batch_id)
    sheet_path = put_object(
        OUTPUT_SHEETS,
        f"{batch_id}.xlsx",
        workbook_bytes,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    db.update_batch(batch_id, {"output_sheet_path": sheet_path, "ticket_count": len(tickets)})
    return {"batch_id": batch_id, "sheet_path": sheet_path, "ticket_count": len(tickets)}
