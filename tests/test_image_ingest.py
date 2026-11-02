"""Regression tests for the POST /images hardening (v1.x prod fix).

Background: a single bad photo used to 500 the whole batch — in prod the trigger
was a Supabase schema error (PGRST204: the `source_filename` column was missing
because db/08 was never applied). The fix isolates each file (a failure becomes a
per-file "error" status instead of a 500) and keeps the PHI gate airtight: if the
redacted image can't be encoded we store NOTHING (never the raw upload), and if
storage fails no ticket row is created at all — the image is written first,
under its own id, so a failed upload leaves nothing half-made behind (2.30.1;
before that the caller's retry loop left one orphan row per attempt).

These tests lock that behavior in. They run hermetically: conftest forces
OFFLINE_MODE so there is no network and schema_check() always passes.
"""
import numpy as np
import pytest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

import app.main as main
import app.pipeline.run as run
from app.db import db
from app.main import app
from app.pipeline.run import ingest_image

from app.pipeline import transient

client = TestClient(app)


def _jpeg_bytes() -> bytes:
    """A tiny real JPEG the pipeline can decode (content is irrelevant — the
    redaction step is patched in tests that need it to reach the encode gate)."""
    import cv2

    img = np.zeros((400, 600, 3), np.uint8)
    img[50:150, 50:550] = 255  # some white content so it isn't a degenerate frame
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


# ---------------------------------------------------------------------------
# 1. Per-file isolation: one bad file must not 500 the whole upload.
# ---------------------------------------------------------------------------
def test_one_bad_file_does_not_fail_the_batch():
    def fake_ingest(data, filename, batch_id):
        if "bad" in filename:
            # Mimic the prod Supabase schema error that triggered the original 500.
            raise Exception(
                "{'code': 'PGRST204', 'message': \"Could not find the "
                "'source_filename' column of 'tickets' in the schema cache\"}"
            )
        return {"ticket_id": "good-ticket", "status": "pending_review"}

    with patch.object(main, "ingest_image", side_effect=fake_ingest):
        r = client.post(
            "/images",
            files=[
                ("files", ("good.jpg", _jpeg_bytes(), "image/jpeg")),
                ("files", ("bad.jpg", _jpeg_bytes(), "image/jpeg")),
            ],
        )

    # The whole request must succeed (202), not 500 on the one bad file.
    assert r.status_code == 202
    body = r.json()
    assert body["batch_id"]
    tickets = body["tickets"]
    assert len(tickets) == 2

    good = next(t for t in tickets if t["filename"] == "good.jpg")
    bad = next(t for t in tickets if t["filename"] == "bad.jpg")

    # The good file is unaffected by its noisy neighbor.
    assert good["status"] == "pending_review"
    assert good["ticket_id"] == "good-ticket"

    # The bad file is reported per-file with an actionable message (not a stack
    # trace) that tells the operator to run the pending migrations.
    assert bad["status"] == "error"
    assert bad["ticket_id"] is None
    assert bad["error"]
    assert "migration" in bad["error"].lower()


# ---------------------------------------------------------------------------
# 2. All-good path still works.
# ---------------------------------------------------------------------------
def test_all_good_files_ingest():
    def fake_ingest(data, filename, batch_id):
        return {"ticket_id": f"tid-{filename}", "status": "pending_review"}

    with patch.object(main, "ingest_image", side_effect=fake_ingest):
        r = client.post(
            "/images",
            files=[
                ("files", ("a.jpg", _jpeg_bytes(), "image/jpeg")),
                ("files", ("b.jpg", _jpeg_bytes(), "image/jpeg")),
            ],
        )

    assert r.status_code == 202
    body = r.json()
    assert body["batch_id"]
    tickets = body["tickets"]
    assert len(tickets) == 2
    assert all(t["status"] == "pending_review" for t in tickets)


# ---------------------------------------------------------------------------
# 3. Unreadable image -> manual_queue, and a ticket never points at nothing.
# ---------------------------------------------------------------------------
def test_encode_failure_routes_to_manual_queue_and_stores_nothing():
    batch = db.create_batch()

    fake_storage = Mock()
    with patch.object(run.preprocess, "encode_image", return_value=b""), \
         patch.object(run, "put_object", fake_storage):
        res = ingest_image(_jpeg_bytes(), "MO-test.jpg", batch["id"])

    # Nothing to store -> manual queue, rather than a ticket whose image
    # reference dangles and whose rows silently come back empty later.
    assert res["status"] == "manual_queue"
    ticket = db.get_ticket(res["ticket_id"])
    assert not ticket.get("source_image_path")  # nothing stored
    fake_storage.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Storage failure -> no ticket row at all, and a named error propagates.
#
# This used to create the ticket first and flip it to manual_queue when the
# upload failed. The caller retries ingest_image, so one hung upload left FOUR
# half-made rows behind — and on 2 Nov a Supabase read timeout did exactly
# that while the person watching got "Internal Server Error".
#
# The image is written first now, under its own id, and the row is created only
# once there are bytes for it to point at. The failure reaches the person
# through the response instead of through a row nobody asked for.
# ---------------------------------------------------------------------------
def test_a_storage_failure_leaves_no_ticket_behind():
    batch = db.create_batch()

    with patch.object(run.preprocess, "encode_image", return_value=b"jpegbytes"), \
         patch.object(run, "put_object", side_effect=Exception("storage down")):
        with pytest.raises(run.StorageUnavailable) as caught:
            ingest_image(_jpeg_bytes(), "MO-store-fail.jpg", batch["id"])

    assert db.tickets_for_batch(batch["id"]) == []
    assert "try again" in str(caught.value), "it has to say whether to retry"


def test_the_upload_is_retried_before_it_is_given_up_on():
    """A momentary storage blip must not cost the photo — that is the 2.22.0
    lesson. What is new is that the retrying is bounded."""
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) < 2:
            raise Exception("Server disconnected")
        return "redacted-images/ok.jpg"

    batch = db.create_batch()
    with patch.object(run.preprocess, "encode_image", return_value=b"jpegbytes"), \
         patch.object(run, "put_object", side_effect=flaky):
        res = ingest_image(_jpeg_bytes(), "MO-flaky.jpg", batch["id"])

    assert len(calls) == 2
    assert db.get_ticket(res["ticket_id"])["source_image_path"] == "redacted-images/ok.jpg"


def test_a_storage_timeout_is_recognised_as_worth_retrying():
    """The classifier is what decides whether the 2 Nov failure retried at all.
    httpcore/httpx name it ReadTimeout; the message says "read operation timed
    out". Both have to match, because which one arrives depends on the layer."""
    from app.pipeline import transient

    class ReadTimeout(Exception):
        pass

    assert transient.is_transient(ReadTimeout("x"))
    assert transient.is_transient(Exception("The read operation timed out"))


def test_the_retry_loop_cannot_outlast_its_budget():
    """Four attempts against a 20s timeout is 84 seconds of somebody watching
    a spinner for an error that was never going to change."""
    import time

    from app.pipeline import transient

    started = time.monotonic()
    with pytest.raises(Exception):
        transient.retry(_always_timing_out, attempts=8, max_seconds=0.8)
    assert time.monotonic() - started < 3.0


def _always_timing_out():
    import time

    time.sleep(0.3)
    raise Exception("The read operation timed out")


# ---------------------------------------------------------------------------
# 5. schema_check is clean offline; /diag reports offline with no probes.
# ---------------------------------------------------------------------------
def test_schema_check_clean_offline_and_diag():
    # The local JSON store has every column implicitly -> no schema problems.
    assert db.schema_check() == []

    r = client.get("/diag")
    assert r.status_code == 200
    body = r.json()
    assert body["datastore"] == "offline"
    # Offline never probes Supabase, so these supabase-only fields are absent.
    assert "schema_problems" not in body
    assert "schema_ok" not in body


# ---------------------------------------------------------------------------
# 6. _explain_db_error turns raw datastore errors into actionable messages.
# ---------------------------------------------------------------------------
def test_explain_db_error_messages():
    # PGRST204 (missing column/table) -> point at the pending migrations in db/.
    pgrst = Exception(
        "{'code': 'PGRST204', 'message': \"Could not find the 'source_filename' "
        "column of 'tickets' in the schema cache\"}"
    )
    msg = main._explain_db_error(pgrst)
    assert "migration" in msg.lower()
    assert "db/" in msg

    # 42501 (row-level security) -> explain the key/RLS problem.
    rls = Exception("42501: new row violates row-level security policy")
    msg = main._explain_db_error(rls)
    assert "row-level security" in msg.lower()

    # Anything else -> generic fallthrough that still surfaces the cause.
    generic = Exception("something unexpected exploded")
    msg = main._explain_db_error(generic)
    assert "could not process the request" in msg.lower()
    assert "something unexpected exploded" in msg


# ---------------------------------------------------------------------------
# Nested retry loops — the bug the memory logs caught
#
# 2.30.1 gave the upload its own retry loop and left the caller's in place.
# StorageUnavailable's message ends "(ReadTimeout)", the classifier matches on
# text as well as type, so the outer loop called it transient and re-ran the
# WHOLE ingest four more times, three uploads apiece. Twelve uploads and ~100
# seconds for one failure, and the production log showed exactly four
# `ingest.start` lines for a single 290 KB file.
# ---------------------------------------------------------------------------
def test_a_storage_failure_is_not_retried_a_second_time():
    """The upload has already done its retrying. An outer loop starting over
    is not more robust, it is the same failure four times."""
    from app.pipeline import transient

    exc = run.StorageUnavailable("... did not respond in time; try again. (ReadTimeout)")
    assert transient.is_transient(exc) is False


def test_a_genuine_timeout_is_still_worth_retrying():
    """The veto must be narrow: the thing StorageUnavailable WRAPS is exactly
    what the inner loop should keep retrying."""
    from app.pipeline import transient

    assert transient.is_transient(Exception("The read operation timed out"))


def test_an_exception_can_declare_itself_final():
    """The general mechanism, so the next layer that wraps a retried failure
    has a way to say so instead of re-discovering this."""
    from app.pipeline import transient

    class Final(Exception):
        retryable = False

    assert transient.is_transient(Final("connection reset")) is False


def test_one_failed_upload_is_one_pass_through_ingest():
    """End to end: the caller's loop must not multiply the inner one."""
    starts = []
    real_decode = run.preprocess.decode_image

    def counting_decode(*a, **k):
        starts.append(1)
        return real_decode(*a, **k)

    batch = db.create_batch()
    with patch.object(run.preprocess, "decode_image", side_effect=counting_decode), \
         patch.object(run.preprocess, "encode_image", return_value=b"jpegbytes"), \
         patch.object(run, "put_object", side_effect=Exception("read operation timed out")):
        with pytest.raises(run.StorageUnavailable):
            transient.retry(ingest_image, _jpeg_bytes(), "MO-nested.jpg",
                            batch["id"], attempts=4)

    assert len(starts) == 1, f"ingest ran {len(starts)} times; the inner loop already retried"


def test_storage_gets_a_longer_timeout_than_a_query():
    """An upload moves megabytes and a query moves kilobytes. Giving both one
    number can only ever cut off a slow-but-working upload."""
    from app.config import Settings

    cfg = Settings(_env_file=None)
    assert cfg.supabase_storage_timeout > cfg.supabase_timeout


# ---------------------------------------------------------------------------
# The Supabase transport
#
# Three probe uploads of 293 KB failed at exactly 20.0s each on a freshly
# restarted container, while postgrest queries went through throughout.
# storage3 builds its httpx client with http2=True, and HTTP/2 opens with a
# 65,535-byte flow-control window: a body larger than that stalls after the
# first 64 KB waiting for a WINDOW_UPDATE and httpx waits out the read timeout.
# "Exactly the timeout, every time" is what waiting looks like.
# ---------------------------------------------------------------------------
def test_http2_is_off_for_supabase_by_default():
    """Anything over 64 KB is every ticket image we store."""
    from app.config import Settings

    assert Settings(_env_file=None).supabase_http2 is False


def test_the_transport_settings_actually_reach_the_client():
    """The 2.30.1 version of this used ClientOptions where SyncClientOptions
    was required — different classes, only the sync one has `storage` — so it
    raised, fell back, and the timeouts never applied in production. A test
    that only checked the setting would not have noticed."""
    import httpx
    from supabase.lib.client_options import SyncClientOptions

    built = {}

    def fake_create_client(url, key, options=None):
        built["options"] = options
        return "client"

    from app.db import _SupabaseBackend

    assert _SupabaseBackend._build_client(fake_create_client) == "client"
    opts = built["options"]
    assert isinstance(opts, SyncClientOptions)
    assert isinstance(opts.httpx_client, httpx.Client)
    # The thing that matters, read off the transport the uploads will use.
    assert opts.httpx_client._transport._pool._http2 is False


def test_a_transport_failure_says_what_it_costs():
    """The old handler said "using the library defaults" without saying which
    defaults or what they mean — so a silent fallback to HTTP/2 read as a
    harmless note for a day."""
    import logging

    from app.db import _SupabaseBackend

    def exploding_create_client(url, key, options=None):
        if options is not None:
            raise TypeError("nope")
        return "fallback"

    logger = logging.getLogger("db")
    records = []
    handler = logging.Handler()
    handler.emit = lambda r: records.append(r.getMessage())
    logger.addHandler(handler)
    try:
        assert _SupabaseBackend._build_client(exploding_create_client) == "fallback"
    finally:
        logger.removeHandler(handler)
    assert any("HTTP/2 is ON" in m for m in records), records
