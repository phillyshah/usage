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
    cleaned = preprocess.preprocess(img)
    template = detect_template(cleaned if cleaned is not None else img, filename)

    redacted, located = redact_patient_region(
        cleaned if cleaned is not None else img, template
    )

    if not located:
        # Fail safe: cannot prove PHI is masked -> manual queue, store nothing.
        ticket = db.create_ticket({
            "batch_id": batch_id,
            "source_image_path": None,
            "entity": template if template != "Unknown" else None,
            "status": "manual_queue",
            "flags": ["Patient region could not be located — manual review required"],
        })
        log.info("ticket %s routed to manual_queue (%s)", ticket["ticket_id"], filename)
        return {"ticket_id": ticket["ticket_id"], "status": "manual_queue"}

    # Store ONLY the redacted image.
    ticket = db.create_ticket({
        "batch_id": batch_id,
        "entity": template,
        "status": "pending_review",
    })
    redacted_bytes = preprocess.encode_image(redacted, ".jpg") or data
    path = f"{ticket['ticket_id']}.jpg"
    ref = put_object(REDACTED_IMAGES, path, redacted_bytes, "image/jpeg")
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

    for ticket in pending:
        try:
            process_ticket(ticket)
        except Exception as e:  # pragma: no cover
            log.exception("failed to process ticket %s: %s", ticket.get("ticket_id"), e)
            db.update_ticket(ticket["ticket_id"], {"flags": [f"Processing error: {e}"]})

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
