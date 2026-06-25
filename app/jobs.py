"""Scheduled jobs (APScheduler): daily batch run + nightly retention purge.

No Celery/Redis at this volume. The scheduler starts with the FastAPI app and
stops with it.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.db import db
from app.storage import delete_object, split_ref

log = logging.getLogger("jobs")
_scheduler: BackgroundScheduler | None = None


def daily_batch_job() -> None:
    """Process any pending tickets and emit the day's review workbook."""
    from app.pipeline.run import run_batch

    try:
        result = run_batch(None)
        log.info("daily batch complete: %s", result)
    except Exception as e:  # pragma: no cover
        log.exception("daily batch failed: %s", e)


def purge_job() -> None:
    """Drop expired per-field snapshots and delete expired redacted images.

    Learned facts are never purged — only the raw originals used for diffing.
    """
    try:
        # 1. Delete redacted image objects for expired tickets (Storage).
        for ticket in db.expired_tickets():
            ref = ticket.get("source_image_path")
            if ref:
                try:
                    bucket, path = split_ref(ref)
                    delete_object(bucket, path)
                    db.update_ticket(ticket["ticket_id"], {"source_image_path": None})
                except Exception as e:  # pragma: no cover
                    log.warning("could not delete image for %s: %s", ticket["ticket_id"], e)
        # 2. Purge per-field snapshots (DB).
        removed = db.purge_expired_field_extractions()
        log.info("purge complete: %s field_extractions removed", removed)
    except Exception as e:  # pragma: no cover
        log.exception("purge failed: %s", e)


def learning_health_job() -> None:
    """Raise the learning-store high-water marks to the current counts.

    The integrity baseline only ever rises here; it lets the History-tab banner
    flag a later shrink (data loss) without false alarms from normal growth.
    """
    from app.metrics import bump_learning_watermarks

    try:
        bump_learning_watermarks()
        log.info("learning watermarks refreshed")
    except Exception as e:  # pragma: no cover
        log.exception("learning watermark refresh failed: %s", e)


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    sched = BackgroundScheduler(timezone="UTC")
    # Daily batch at 02:00, purge at 03:00 (mirrors the pg_cron purge schedule),
    # learning-integrity snapshot at 03:30 (after the purge, which never touches
    # the learning stores).
    sched.add_job(daily_batch_job, "cron", hour=2, minute=0, id="daily_batch")
    sched.add_job(purge_job, "cron", hour=3, minute=0, id="purge")
    sched.add_job(learning_health_job, "cron", hour=3, minute=30, id="learning_health")
    sched.start()
    _scheduler = sched
    log.info("scheduler started (daily batch 02:00 UTC, purge 03:00 UTC, "
             "learning snapshot 03:30 UTC)")
    return sched


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
