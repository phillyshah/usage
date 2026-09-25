"""The weekday status email.

The feature exists because a day nobody runs the app produces no error, no
empty report, nothing at all — so it stays invisible until month end. These
tests pin the three things that make it trustworthy: it measures the right
signal, it measures it on the right day, and it cannot leak PHI.
"""
import json
from dataclasses import fields
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import notify
from app.db import db
from app.email import EmailConfig, build_message, email_configuration, recipients
from app.main import app
from app.notify import DayStatus, collect_status, render

client = TestClient(app)

EASTERN_OFFSET = "-04:00"   # EDT; the June dates below are always in DST


@pytest.fixture(autouse=True)
def _clean():
    for key in (notify.ENABLED_KEY, notify.RECIPIENTS_KEY, notify.LAST_STATUS_KEY):
        db.set_app_setting(key, "")
    yield


def _ticket(created_at: str, status: str = "pending_review", **extra):
    batch = db.create_batch()
    row = {"batch_id": batch["id"], "source_filename": "MO1.jpg",
           "status": status, "created_at": created_at}
    row.update(extra)
    return db.create_ticket(row)


# --------------------------------------------------------------------------
# Configuration: a reason, never an exception
# --------------------------------------------------------------------------
def test_unconfigured_email_returns_a_reason_naming_the_missing_value():
    """'Not set up yet' is the state every install starts in, so the card has
    to be able to say which value is missing rather than showing a traceback."""
    config, reason = email_configuration()
    assert config is None
    assert "EMAIL_FROM" in reason


def test_smtp_needs_host_user_and_password(monkeypatch):
    class Cfg:
        email_provider, email_from, email_api_key = "smtp", "usage@maxx.com", ""
        smtp_host, smtp_port, smtp_user, smtp_password = "", 465, "", ""

    assert "SMTP_HOST" in email_configuration(Cfg)[1]
    Cfg.smtp_host = "mail.example.com"
    assert "SMTP_USER" in email_configuration(Cfg)[1]
    Cfg.smtp_user, Cfg.smtp_password = "u", "p"
    config, reason = email_configuration(Cfg)
    assert reason is None and config.host == "mail.example.com" and config.port == 465


def test_a_provider_this_app_cannot_send_with_says_so_plainly():
    """The dashboard's .env may name resend; this app only has an SMTP path,
    and a silent no-op would be worse than a sentence."""
    class Cfg:
        email_provider, email_from, email_api_key = "resend", "usage@maxx.com", "key"
        smtp_host = smtp_user = smtp_password = ""
        smtp_port = 465

    config, reason = email_configuration(Cfg)
    assert config is None and "only sends over SMTP" in reason


def test_recipients_are_split_deduplicated_and_validated():
    assert recipients("a@x.com, b@y.com;a@x.com\nc@z.com") == \
        ["a@x.com", "b@y.com", "c@z.com"]
    assert recipients("not-an-address") == []
    assert recipients(None) == []


# --------------------------------------------------------------------------
# The signal: uploads, not batches; and the team's day, not the server's
# --------------------------------------------------------------------------
def test_the_alarm_keys_off_uploads_not_batches():
    """daily_batch_job runs unattended at 02:00 UTC and makes a batch out of
    whatever was pending, so a batch can exist with nobody having touched the
    app. Uploading is the part only a person can do."""
    today = date(2026, 6, 10)
    db.create_batch()   # a batch today, but no tickets uploaded
    status = collect_status(today)
    assert not status.ran
    assert "NOTHING RUN TODAY" in render(status)[0]


def test_uploaded_but_never_processed_is_its_own_state():
    """The team did their part and the output is still sitting there — that is
    a different message from nobody showing up."""
    status = DayStatus("2026-06-10", tickets_uploaded=12, batches_generated=0,
                       tickets_pending=12, tickets_verified=0, price_runs=0,
                       consecutive_misses=0)
    assert status.has_unprocessed
    assert "none processed" in render(status)[0]


def test_a_normal_day_reports_the_counts():
    status = DayStatus("2026-06-10", tickets_uploaded=47, batches_generated=3,
                       tickets_pending=10, tickets_verified=37, price_runs=1,
                       consecutive_misses=0)
    subject, text, _ = render(status)
    assert subject == "Usage Wed 10 Jun: 3 batches, 47 tickets"
    assert "Tickets uploaded: 47" in text


def test_evening_work_counts_against_the_day_the_team_worked():
    """batches.run_date defaults to Postgres current_date, which is UTC and
    rolls over at 8pm Eastern — so a ticket uploaded at 9pm Eastern on the 10th
    is stamped the 11th. The window has to be computed in the team's zone."""
    _ticket(f"2026-06-10T21:30:00{EASTERN_OFFSET}")     # 9:30pm Wed Eastern
    assert collect_status(date(2026, 6, 10)).tickets_uploaded == 1
    assert collect_status(date(2026, 6, 11)).tickets_uploaded == 0


def test_consecutive_misses_skip_weekends():
    """A Monday with nothing uploaded is the 1st weekday missed, not the 3rd —
    nobody was meant to be working on Saturday."""
    monday = date(2026, 6, 15)
    assert monday.weekday() == 0
    _ticket(f"2026-06-12T10:00:00{EASTERN_OFFSET}")     # the Friday before
    assert collect_status(monday).consecutive_misses == 1


def test_a_run_of_missed_days_is_named_in_the_subject():
    status = DayStatus("2026-06-10", 0, 0, 0, 0, 0, consecutive_misses=3)
    assert "(3rd weekday)" in render(status)[0]
    assert "1st" in render(DayStatus("2026-06-10", 0, 0, 0, 0, 0, 1))[0] or \
        render(DayStatus("2026-06-10", 0, 0, 0, 0, 0, 1))[0].endswith("TODAY")


# --------------------------------------------------------------------------
# PHI — the safeguard is the type, not the template
# --------------------------------------------------------------------------
def test_daystatus_can_only_hold_counts():
    """The real guarantee: every field is a date or an int, so there is no
    field capable of carrying a hospital, a surgeon or a patient's initials.
    If someone widens this dataclass, this test is the thing that objects."""
    kinds = {f.name: f.type for f in fields(DayStatus)}
    assert kinds.pop("day") == "str"                      # the ISO date
    assert set(kinds.values()) == {"int"}, kinds


def test_no_patient_surgeon_or_hospital_string_reaches_the_email():
    # Its own date: the local store is shared across tests in a session, so a
    # day another test also writes to would make the count assertion flap.
    secrets = {"hospital": "Blake Hospital", "surgeon": "Woodworth",
               "patient_initials": "JS", "rep_code": "GR-ME-001"}
    _ticket(f"2026-06-17T10:00:00{EASTERN_OFFSET}", **secrets)

    status = collect_status(date(2026, 6, 17))
    subject, text, html = render(status)
    blob = f"{subject}\n{text}\n{html}"
    for label, value in secrets.items():
        assert value not in blob, f"{label} leaked into the status email"
    assert status.tickets_uploaded == 1                   # it did see the ticket


# --------------------------------------------------------------------------
# Sending, and the record of it
# --------------------------------------------------------------------------
def test_nothing_is_sent_while_notifications_are_off():
    db.set_app_setting(notify.RECIPIENTS_KEY, "boss@maxx.com")
    db.set_app_setting(notify.ENABLED_KEY, "false")
    assert notify.send_daily_status() == {"sent": False,
                                          "reason": "notifications are turned off"}


def test_the_scheduled_job_does_not_send_at_the_weekend():
    db.set_app_setting(notify.RECIPIENTS_KEY, "boss@maxx.com")
    db.set_app_setting(notify.ENABLED_KEY, "true")
    saturday = datetime(2026, 6, 13, 17, 0, tzinfo=timezone.utc)
    with patch("app.notify.datetime") as dt:
        dt.now.return_value = saturday
        assert notify.send_daily_status()["reason"] == "not a weekday"


def test_a_failed_send_is_recorded_so_the_monitor_is_not_itself_silent():
    """A notifier that dies quietly is the exact failure it exists to catch."""
    db.set_app_setting(notify.RECIPIENTS_KEY, "boss@maxx.com")
    with patch("app.email.send_email", return_value=(False, "Login denied")):
        record = notify.send_daily_status(force=True)
    assert record["sent"] is False and record["detail"] == "Login denied"
    assert notify.last_send()["detail"] == "Login denied"


def test_a_successful_send_records_the_subject_and_counts():
    db.set_app_setting(notify.RECIPIENTS_KEY, "boss@maxx.com")
    with patch("app.email.send_email", return_value=(True, "Sent to 1 recipient(s).")):
        record = notify.send_daily_status(force=True)
    assert record["sent"] is True
    assert record["subject"].startswith("Usage ")
    assert "tickets_uploaded" in record["status"]


def test_the_message_carries_both_a_text_and_an_html_part():
    msg = build_message(EmailConfig("smtp", "usage@maxx.com"), ["a@x.com"],
                        "Subject", "plain body", "<p>rich body</p>")
    assert msg["To"] == "a@x.com"
    assert {p.get_content_type() for p in msg.walk()} >= {"text/plain", "text/html"}


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
def test_turning_it_on_without_a_recipient_is_refused():
    r = client.put("/notifications", json={"enabled": True})
    assert r.status_code == 400
    assert "at least one email address" in r.json()["detail"]


def test_a_malformed_address_is_refused_rather_than_silently_dropped():
    r = client.put("/notifications", json={"recipients": "boss@maxx.com, oops"})
    assert r.status_code == 400
    assert db.get_app_setting(notify.RECIPIENTS_KEY) in (None, "")


def test_recipients_round_trip_and_the_schedule_is_stated_in_words():
    r = client.put("/notifications", json={"recipients": "boss@maxx.com"})
    assert r.status_code == 200
    body = r.json()
    assert body["recipients"] == ["boss@maxx.com"]
    assert body["schedule"] == "5pm Eastern time"
    assert body["email_configured"] is False       # no relay in the test env
    assert "EMAIL_FROM" in body["reason"]


def test_the_test_button_reports_the_relays_own_words():
    """Setup fails in ways only the relay knows about. Finding that out at 5pm
    on a day nobody ran the app is too late."""
    client.put("/notifications", json={"recipients": "boss@maxx.com"})
    with patch("app.email.send_email", return_value=(False, "Login denied")):
        r = client.post("/notifications/test")
    assert r.status_code == 400 and r.json()["detail"] == "Login denied"


def test_diag_reports_the_notifiers_own_health():
    assert "notifications" in client.get("/diag").json()
