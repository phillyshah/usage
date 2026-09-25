"""The weekday status email: did the team actually run the app today?

The problem this solves is a silent one. Nobody notices a day that didn't
happen until month-end, because not running the app produces no error, no empty
report, nothing at all. So the app says so itself, every weekday at 5pm.

Two decisions worth knowing about:

**The day is the team's day, not the server's.** ``batches.run_date`` defaults
to Postgres ``current_date``, which is UTC and rolls over at 8pm Eastern — so
work done on a Thursday evening is stamped Friday. Every window here is computed
in the configured timezone from ``created_at``, or the 4pm-to-midnight shift
would be reported against the wrong day.

**The alarm keys off tickets uploaded, not batches generated.** ``daily_batch_job``
runs unattended at 02:00 UTC, which is 10pm Eastern the evening before, and
creates a batch out of whatever was left pending. So "a batch exists today" can
be true with nobody having touched the app. Uploading tickets is the part only a
person can do, so that is what silence is measured against.

**Nothing here may carry PHI.** The email is rendered from ``DayStatus``, which
holds a date and six integers and has no field capable of holding a hospital, a
surgeon or a patient's initials. That is the safeguard — not the wording of the
template. See ``tests/test_notify.py``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import settings
from app.db import db

log = logging.getLogger("notify")

# app_settings keys. app_settings already exists (db/01), so no migration.
ENABLED_KEY = "notify_enabled"
RECIPIENTS_KEY = "notify_emails"
LAST_STATUS_KEY = "notify_last_status"


@dataclass(frozen=True)
class DayStatus:
    """One weekday, in counts only. Deliberately incapable of holding PHI."""
    day: str                      # ISO date, in the team's timezone
    tickets_uploaded: int
    batches_generated: int
    tickets_pending: int
    tickets_verified: int
    price_runs: int
    consecutive_misses: int       # weekdays in a row with no uploads, incl. today

    @property
    def ran(self) -> bool:
        return self.tickets_uploaded > 0

    @property
    def has_unprocessed(self) -> bool:
        """Work was uploaded but never turned into a workbook. Its own state:
        the team did their part and the output is still sitting there."""
        return self.tickets_uploaded > 0 and self.batches_generated == 0


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.notify_timezone)
    except Exception:
        log.warning("unknown notify_timezone %r; falling back to UTC",
                    settings.notify_timezone)
        return ZoneInfo("UTC")


def _parse(ts) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=ZoneInfo("UTC")) if dt.tzinfo is None else dt


def _local_day(ts, tz: ZoneInfo) -> str | None:
    dt = _parse(ts)
    return dt.astimezone(tz).date().isoformat() if dt else None


def _is_weekday(d) -> bool:
    return d.weekday() < 5


def collect_status(today=None) -> DayStatus:
    """Count what happened on ``today`` (a date in the team's timezone)."""
    tz = _tz()
    today = today or datetime.now(tz).date()
    key = today.isoformat()

    tickets = db.backend.select("tickets")
    by_day: dict[str, list[dict]] = {}
    for t in tickets:
        d = _local_day(t.get("created_at"), tz)
        if d:
            by_day.setdefault(d, []).append(t)
    todays = by_day.get(key, [])

    batches = sum(1 for b in db.backend.select("batches")
                  if _local_day(b.get("created_at"), tz) == key)
    try:
        price_runs = sum(1 for r in db.backend.select("pricing_runs")
                         if _local_day(r.get("created_at"), tz) == key
                         and r.get("status") == "succeeded")
    except Exception:
        price_runs = 0

    # Consecutive weekdays with no uploads, counting back from today. Weekends
    # are skipped rather than counted, so a Monday miss reads "1st", not "3rd".
    misses = 0
    probe = today
    while True:
        if _is_weekday(probe):
            if by_day.get(probe.isoformat()):
                break
            misses += 1
            # Cap the walk: on a fresh install nothing has ever been uploaded,
            # and there is no point counting back through all of history to say
            # so. "30+" reads the same as any larger number.
            if misses >= 30:
                break
        probe -= timedelta(days=1)

    return DayStatus(
        day=key,
        tickets_uploaded=len(todays),
        batches_generated=batches,
        tickets_pending=sum(1 for t in todays if t.get("status") == "pending_review"),
        tickets_verified=sum(1 for t in todays if t.get("status") == "verified"),
        price_runs=price_runs,
        consecutive_misses=misses,
    )


# Zone names are for machines. The card is read by the person deciding whether
# 5pm is the right time, so it says "5pm Eastern time".
_ZONE_LABELS = {
    "America/New_York": "Eastern time",
    "America/Chicago": "Central time",
    "America/Denver": "Mountain time",
    "America/Los_Angeles": "Pacific time",
    "America/Phoenix": "Arizona time",
    "UTC": "UTC",
}


def schedule_label() -> str:
    """e.g. "5pm Eastern time" — the schedule as a person would say it."""
    hour = int(settings.notify_hour)
    suffix = "am" if hour < 12 else "pm"
    display = hour % 12 or 12
    zone = _ZONE_LABELS.get(settings.notify_timezone, settings.notify_timezone)
    return f"{display}{suffix} {zone}"


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def render(status: DayStatus, app_url: str = "https://usage.90ten.life") -> tuple[str, str, str]:
    """``(subject, text, html)``.

    The subject carries the state, so the answer is readable in a notification
    without opening anything — which is the difference between a thing people
    act on and a thing people filter.
    """
    pretty = datetime.fromisoformat(status.day).strftime("%a %d %b")

    if not status.ran:
        nth = f" ({_ordinal(status.consecutive_misses)} weekday)" \
            if status.consecutive_misses > 1 else ""
        subject = f"Usage {pretty}: NOTHING RUN TODAY{nth}"
        lead = ("No tickets were uploaded today. If surgeries happened, the "
                "tickets for them have not been entered.")
        if status.consecutive_misses > 1:
            lead += (f" This is the {_ordinal(status.consecutive_misses)} weekday "
                     "in a row with nothing uploaded.")
    elif status.has_unprocessed:
        subject = (f"Usage {pretty}: {status.tickets_uploaded} tickets uploaded, "
                   "none processed")
        lead = ("Tickets were uploaded but no spreadsheet was generated, so the "
                "work is sitting unprocessed.")
    else:
        subject = (f"Usage {pretty}: {status.batches_generated} batch"
                   f"{'' if status.batches_generated == 1 else 'es'}, "
                   f"{status.tickets_uploaded} tickets")
        lead = "Normal day — tickets were uploaded and processed."

    rows = [
        ("Tickets uploaded", status.tickets_uploaded),
        ("Spreadsheets generated", status.batches_generated),
        ("Awaiting review", status.tickets_pending),
        ("Verified", status.tickets_verified),
        ("Price enrichment runs", status.price_runs),
    ]
    text = "\n".join([
        lead, "",
        *(f"  {label}: {value}" for label, value in rows),
        "", f"Open the app: {app_url}",
        "", "This is an automated status message from the Usage app. It contains "
            "counts only — no patient, surgeon or hospital information.",
    ])
    tr = "".join(
        f'<tr><td style="padding:6px 16px 6px 0;color:#4a5b63">{label}</td>'
        f'<td style="padding:6px 0;font-weight:700;text-align:right">{value}</td></tr>'
        for label, value in rows)
    html = (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;'
        'font-size:15px;color:#16242b;line-height:1.5">'
        f'<p style="margin:0 0 16px">{lead}</p>'
        f'<table style="border-collapse:collapse;margin:0 0 20px">{tr}</table>'
        f'<p style="margin:0 0 16px"><a href="{app_url}" '
        'style="background:#16242b;color:#fff;padding:10px 18px;border-radius:6px;'
        'text-decoration:none;display:inline-block">Open the app</a></p>'
        '<p style="margin:0;color:#7b8a92;font-size:13px">Automated status message '
        'from the Usage app. Counts only — no patient, surgeon or hospital '
        'information.</p></div>'
    )
    return subject, text, html


def send_daily_status(force: bool = False) -> dict:
    """Collect, render and send. Returns a record of what happened.

    ``force`` skips the weekday and enabled checks so the Notifications card can
    put a real message on the wire on demand — waiting until 5pm to discover the
    password is wrong is not a test.
    """
    from app.email import recipients, send_email

    tz = _tz()
    today = datetime.now(tz).date()
    if not force:
        if (db.get_app_setting(ENABLED_KEY) or "").lower() not in ("1", "true", "yes"):
            return {"sent": False, "reason": "notifications are turned off"}
        if not _is_weekday(today):
            return {"sent": False, "reason": "not a weekday"}

    to = recipients(db.get_app_setting(RECIPIENTS_KEY))
    status = collect_status(today)
    subject, text, html = render(status)
    ok, detail = send_email(to, subject, text, html)

    record = {"sent": ok, "detail": detail, "subject": subject,
              "at": datetime.now(tz).isoformat(timespec="seconds"),
              "status": asdict(status)}
    try:
        import json
        db.set_app_setting(LAST_STATUS_KEY, json.dumps(record))
    except Exception as exc:  # never let bookkeeping break the send
        log.warning("could not record notify status: %s", exc)
    log.info("daily status: sent=%s %s", ok, detail)
    return record


def last_send() -> dict | None:
    """The last attempt, for the UI and /diag. A notifier that fails quietly is
    the same failure it exists to catch."""
    import json
    raw = db.get_app_setting(LAST_STATUS_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None
