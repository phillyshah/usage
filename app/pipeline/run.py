"""Orchestration: ingest one image and run a batch.

Ingest (`ingest_image`) is used by POST /images:
    bytes (in memory) -> preprocess -> detect template -> store -> pending_review

Batch processing (`run_batch`) is used by POST /batches/run and the scheduler:
    for each pending ticket -> load image -> decode barcodes
      -> resolve refs -> vision read -> score + persist -> write workbook

`run_batch` reports how many tickets came back with nothing read. That number
is the difference between a finished batch and a batch that only looks finished:
barcodes decode locally, so a total failure of the vision read still produces a
full-looking spreadsheet with every handwritten field blank.
"""
from __future__ import annotations

import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor

from app.config import settings
from app.db import db
from app.pipeline import assemble, barcode, preprocess, vision
from app.pipeline import transient
from app.pipeline.template import detect_template, geometry_for
from app.storage import TICKET_IMAGES, get_object, put_object, split_ref

log = logging.getLogger("pipeline.run")


class VisionUnavailable(RuntimeError):
    """The AI reader could not be reached, so the batch did not start.

    Deliberately raised rather than swallowed: every ticket in the batch would
    come back with nothing handwritten read, and the tickets stay
    pending_review so a re-run picks them up untouched.
    """


def _is_transient(exc: Exception) -> bool:
    """Retryable? Delegates to the shared rule so the vision call and this
    retry loop can never disagree about what counts as transient."""
    return transient.is_transient(exc)


def ingest_image(data: bytes, filename: str, batch_id: str) -> dict:
    """Store one ticket image. Returns {ticket_id, status}.

    There used to be a patient-sticker mask here, and a gate that sent a ticket
    to the manual queue whenever the mask could not be proven to have landed.
    Both are gone: storage is HIPAA-compliant, so masking was not buying the
    protection it cost. What it cost was real — the band was positioned by fixed
    fractional coordinates, so on a differently-framed photo it clipped the
    Surgery Date and the Surgeon out of the header, and a ticket that failed the
    gate produced zero rows in the deliverable while still appearing complete.
    """
    img = preprocess.decode_image(data)
    template = detect_template(img, filename)

    # Encode before persisting anything, so a ticket never points at bytes we
    # could not write. A re-encode also normalises whatever the phone produced
    # into the one format the rest of the pipeline expects.
    stored_bytes = preprocess.encode_image(img, ".jpg") if img is not None else b""
    if not stored_bytes:
        ticket = db.create_ticket({
            "batch_id": batch_id,
            "source_image_path": None,
            "source_filename": filename or None,
            "entity": template if template != "Unknown" else None,
            "status": "manual_queue",
            "flags": ["Could not read this image — manual review required"],
        })
        log.info("ticket %s routed to manual_queue (undecodable, %s)",
                 ticket["ticket_id"], filename)
        return {"ticket_id": ticket["ticket_id"], "status": "manual_queue"}

    ticket = db.create_ticket({
        "batch_id": batch_id,
        "entity": template,
        "source_filename": filename or None,
        "status": "pending_review",
    })
    try:
        ref = put_object(TICKET_IMAGES, f"{ticket['ticket_id']}.jpg",
                         stored_bytes, "image/jpeg")
    except Exception:
        db.update_ticket(ticket["ticket_id"], {
            "status": "manual_queue",
            "flags": ["Could not store the image — manual review required"],
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
    image_bytes = b""
    ref = ticket.get("source_image_path")
    if ref:
        try:
            bucket, path = split_ref(ref)
            image_bytes = get_object(bucket, path)
            img = preprocess.decode_image(image_bytes)
        except Exception as e:  # pragma: no cover
            log.warning("could not load the image for %s: %s", ticket_id, e)

    template = ticket.get("entity") or "Maxx Orthopedics"

    # Deterministic first: decode device labels from the grid region. Junk
    # payloads (e.g. a patient wristband barcode) are filtered out before the
    # vision merge so they can't occupy a line slot or shift the pairing.
    grid = _grid_crop(img, template)
    labels = barcode.drop_junk_labels(barcode.decode_region(grid)) if grid is not None else []

    # The vision read: header, prices, qty, totals. One call per ticket.
    vresult = vision.extract_handwritten(image_bytes)

    # If vision returned more priced lines than decoded labels, pad with empty
    # label dicts so vision-only lines still appear (barcode failed on those).
    vlines = vresult.get("lines", []) if vresult else []
    while len(labels) < len(vlines):
        labels.append({"gtin": None, "lot": None, "expiry": None, "mfg": None, "serial": None, "raw": None, "decoded": False, "ref": None})

    summary = assemble.assemble_and_persist(ticket, vresult, labels)
    return summary


def _safe_process(ticket: dict, attempts: int = 4) -> dict:
    """Process one ticket, retrying transient connection failures.

    The shared Supabase HTTP/2 client throws ConnectionTerminated (GOAWAY) when
    several tickets run at once; re-processing is idempotent (assemble clears the
    ticket's prior rows first), so a retry cleanly replaces any partial write.
    A non-transient error (or the final attempt) flags the ticket and returns.

    Returns the ticket summary so run_batch can report how the batch actually
    went — in particular how many tickets came back with nothing read.
    """
    for attempt in range(attempts):
        try:
            return process_ticket(ticket)
        except Exception as e:  # pragma: no cover - network-timing dependent
            if attempt < attempts - 1 and _is_transient(e):
                time.sleep(0.5 * (2 ** attempt) + random.random() * 0.3)
                continue
            log.exception("failed to process ticket %s: %s", ticket.get("ticket_id"), e)
            db.update_ticket(ticket["ticket_id"], {"flags": [f"Processing error: {e}"]})
            return {"ticket_id": ticket.get("ticket_id"), "vision_error": str(e)}


def run_batch(batch_id: str | None = None) -> dict:
    """Process all pending tickets (optionally just one batch) and write the sheet."""
    from app.sheets.write import write_review_workbook
    from app.storage import OUTPUT_SHEETS

    # Preflight. A batch that cannot read anything should not consume the
    # tickets and hand back a barcode-only spreadsheet that looks finished: one
    # small call answers in seconds, where nine tickets answered in minutes and
    # left every one of them marked processed. Skipped in OFFLINE_MODE, where
    # the deterministic path is the whole point.
    if settings.has_anthropic:
        probe = vision.check_connection()
        if not probe["ok"]:
            log.error("batch aborted — the AI reader is unreachable: %s", probe["error"])
            raise VisionUnavailable(probe["error"] or "the AI reader is unreachable")

    pending = db.pending_tickets(batch_id)
    if not pending and batch_id:
        # Re-run on an already-processed batch: include its tickets for the sheet.
        pending = []

    # Process tickets concurrently: each ticket's work is barcode decode (native,
    # releases the GIL), one vision API call (network), and bulk DB writes
    # (network) — all I/O-bound, so threads overlap the latency. Capped to keep
    # the vision API within sane concurrency.
    vision_failures = 0
    if pending:
        # Cap concurrency low: the work shares one Supabase HTTP/2 client, and too
        # many simultaneous tickets trigger GOAWAY/ConnectionTerminated. Retries
        # cover the residual; bulk writes keep each ticket cheap regardless.
        with ThreadPoolExecutor(max_workers=min(3, len(pending))) as ex:
            summaries = list(ex.map(_safe_process, pending))
        vision_failures = sum(1 for r in summaries if (r or {}).get("vision_error"))
        if vision_failures:
            log.error("%d of %d tickets came back with nothing read",
                      vision_failures, len(pending))

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
    return {"batch_id": batch_id, "sheet_path": sheet_path,
            "ticket_count": len(tickets), "vision_failures": vision_failures}
