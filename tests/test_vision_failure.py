"""A failed extraction must never look like a blank ticket.

This is the regression for the batch that started all of this. Nine real
tickets went through, the vision call returned nothing on every one of them,
and the run reported success — because barcodes decode locally, so the
spreadsheet still came out looking full. 54% of the deliverable was red, and
nothing anywhere said why: `except Exception: return _EMPTY` bound no variable,
logged nothing, flagged nothing and retried nothing, and the three empty
results (no API key, API error, unparseable response) were byte-identical to a
genuinely empty ticket.

So these tests are mostly about what the system SAYS, not what it computes.
"""
import json
from contextlib import ExitStack, contextmanager
from unittest.mock import MagicMock, patch

import pytest

from app.db import db
from app.pipeline import vision
from app.pipeline.assemble import assemble_and_persist


def _f(value, confidence="high"):
    return {"value": value, "confidence": confidence}


def _client(*, raises=None, text=None, stop_reason="end_turn"):
    c = MagicMock()
    if raises is not None:
        c.messages.create.side_effect = raises
    else:
        resp = MagicMock()
        resp.content = [MagicMock(type="text", text=text)]
        resp.stop_reason = stop_reason
        resp.usage = MagicMock(input_tokens=10, output_tokens=5,
                               cache_read_input_tokens=0,
                               cache_creation_input_tokens=0)
        c.messages.create.return_value = resp
    return c


@contextmanager
def _live(client):
    """Patch enough that extract_handwritten takes the real Anthropic path.

    The provider is pinned: OpenRouter is the default now, and these tests are
    about the Anthropic transport specifically.
    """
    import anthropic

    with ExitStack() as stack:
        for ctx in (
            patch.object(vision.settings, "vision_provider", "anthropic"),
            patch.object(vision.settings, "anthropic_api_key", "sk-test"),
            patch.object(vision.settings, "offline_mode", False),
            patch.object(anthropic, "Anthropic", return_value=client),
        ):
            stack.enter_context(ctx)
        yield client


def _extract(client):
    with _live(client):
        return vision.extract_handwritten(b"jpegbytes")


# ---------------------------------------------------------------------------
# The empty result always says why it is empty
# ---------------------------------------------------------------------------
def test_an_api_error_is_reported_not_swallowed():
    result = _extract(_client(raises=Exception("api down")))
    assert result["error"], "an empty result with no reason is indistinguishable from a blank ticket"
    assert "api down" in result["error"]


def test_a_truncated_response_is_reported_as_an_error():
    """Thinking and output share one max_tokens allowance, so a long
    deliberation can cut the JSON off mid-object. That used to come back as a
    silent empty result — after a billed call."""
    result = _extract(_client(text='{"header": {"surgeon": {"val',
                              stop_reason="max_tokens"))
    assert result["error"] and "truncated" in result["error"]


def test_a_non_json_response_is_reported_as_an_error():
    result = _extract(_client(text="Sure! Here are the fields you asked for."))
    assert result["error"] and "unparseable" in result["error"]


def test_a_good_response_carries_no_error():
    payload = json.dumps({"header": {"surgeon": {"value": "Konkin",
                                                 "confidence": "high"}},
                          "lines": [], "freight": {"value": None, "confidence": "low"},
                          "grand_total": {"value": 2550, "confidence": "high"}})
    result = _extract(_client(text=payload))
    assert not result.get("error")
    assert result["header"]["surgeon"]["value"] == "Konkin"


def test_offline_mode_is_not_an_error():
    """The deterministic path is the whole point of OFFLINE_MODE — a deliberate
    choice, not an outage."""
    with patch.object(vision.settings, "offline_mode", True):
        assert vision.extract_handwritten(b"x").get("error") is None


def test_a_missing_key_in_a_live_deployment_is_an_error():
    with patch.object(vision.settings, "offline_mode", False), \
         patch.object(vision.settings, "anthropic_api_key", ""):
        assert vision.extract_handwritten(b"x")["error"]


# ---------------------------------------------------------------------------
# Transient failures reach the retry loop that was written for them
# ---------------------------------------------------------------------------
def test_a_transient_failure_is_raised_so_the_ticket_is_retried():
    """_safe_process already retries 4x with backoff and _is_transient already
    lists RateLimitError / 529 / "overloaded". None of it could ever fire,
    because this handler swallowed them first."""
    class RateLimitError(Exception):
        pass

    with _live(_client(raises=RateLimitError("slow down"))), \
         pytest.raises(RateLimitError):
        vision.extract_handwritten(b"jpegbytes")


def test_a_permanent_failure_is_not_raised_and_does_not_sink_the_batch():
    result = _extract(_client(raises=ValueError("bad request")))
    assert result["error"] and result["lines"] == []


# ---------------------------------------------------------------------------
# The ticket, the workbook and the batch all say so
# ---------------------------------------------------------------------------
def test_a_failed_ticket_is_flagged_loudly():
    batch = db.create_batch()
    t = db.create_ticket({"batch_id": batch["id"], "source_filename": "MO-fail.jpg",
                          "status": "pending_review"})
    summary = assemble_and_persist(db.get_ticket(t["ticket_id"]), {
        "header": {}, "lines": [], "freight": _f(None, "low"),
        "grand_total": _f(None, "low"), "error": "APIStatusError: 500",
    }, [])
    joined = " ".join(summary["flags"])
    assert "EXTRACTION FAILED" in joined
    assert "500" in joined
    assert summary["vision_error"] == "APIStatusError: 500"
    # And it reaches the reviewer, not just the return value.
    assert any("EXTRACTION FAILED" in str(f)
               for f in db.get_ticket(t["ticket_id"])["flags"])


def test_a_genuinely_empty_ticket_is_not_flagged_as_failed():
    """The distinction is the entire point — don't cry wolf on a blank form."""
    batch = db.create_batch()
    t = db.create_ticket({"batch_id": batch["id"], "source_filename": "MO-blank.jpg",
                          "status": "pending_review"})
    summary = assemble_and_persist(db.get_ticket(t["ticket_id"]), {
        "header": {}, "lines": [], "freight": _f(None, "low"),
        "grand_total": _f(None, "low"),
    }, [])
    assert not any("EXTRACTION FAILED" in f for f in summary["flags"])
    assert summary["vision_error"] is None


def test_the_status_email_counts_unread_tickets():
    from app.notify import DayStatus, render

    status = DayStatus("2026-06-10", tickets_uploaded=9, batches_generated=1,
                       tickets_pending=9, tickets_verified=0, price_runs=0,
                       consecutive_misses=0, tickets_unread=9)
    assert status.has_unread
    subject, text, _ = render(status)
    assert "COULD NOT BE READ" in subject
    assert "Could not be read: 9" in text


# ---------------------------------------------------------------------------
# Preflight: find a broken configuration in seconds, not in nine tickets
# ---------------------------------------------------------------------------
def test_check_connection_reports_the_exact_error():
    """The error text is the diagnosis — it must not be paraphrased away."""
    with _live(_client(raises=Exception("model: claude-nope not found"))):
        result = vision.check_connection()
    assert result["ok"] is False
    assert "claude-nope not found" in result["error"]


def test_check_connection_sends_the_same_parameters_as_a_real_extraction():
    """If it sent a different shape it could not catch a rejected parameter,
    which is the only reason it exists."""
    client = _client(text="ok")
    with _live(client):
        vision.check_connection()
    probe = client.messages.create.call_args.kwargs

    real = _client(text='{"header": {}, "lines": [], "freight": null, "grand_total": null}')
    with _live(real):
        vision.extract_handwritten(b"jpegbytes")
    live = real.messages.create.call_args.kwargs

    for key in ("model", "thinking", "output_config"):
        assert probe[key] == live[key], f"preflight drifted from the real call on {key!r}"


def test_check_connection_passes_when_the_api_answers():
    with _live(_client(text="ok")):
        result = vision.check_connection()
    assert result["ok"] is True and result["error"] is None


def test_offline_mode_needs_no_connection():
    with patch.object(vision.settings, "offline_mode", True):
        assert vision.check_connection()["ok"] is True


def test_a_missing_key_fails_the_check_without_calling_out():
    client = _client(text="ok")
    with patch.object(vision.settings, "offline_mode", False), \
         patch.object(vision.settings, "anthropic_api_key", ""):
        result = vision.check_connection()
    assert result["ok"] is False
    client.messages.create.assert_not_called()


def test_a_refusal_is_reported_as_a_refusal_not_as_bad_json():
    """A declined request is HTTP 200 with no text. Reported as "unparseable"
    it would send the next person hunting in the wrong place."""
    client = _client(text="", stop_reason="refusal")
    client.messages.create.return_value.stop_details = MagicMock(category="cyber")
    result = _extract(client)
    assert "declined" in result["error"] and "cyber" in result["error"]


# ---------------------------------------------------------------------------
# A batch that cannot read anything does not consume the tickets
# ---------------------------------------------------------------------------
def test_a_failed_preflight_aborts_the_batch_and_touches_nothing():
    import app.pipeline.run as run

    batch = db.create_batch()
    t = db.create_ticket({"batch_id": batch["id"], "source_filename": "MO-abort.jpg",
                          "status": "pending_review"})
    with patch.object(run.settings, "vision_provider", "anthropic"), \
         patch.object(run.settings, "anthropic_api_key", "sk-test"), \
         patch.object(run.settings, "offline_mode", False), \
         patch.object(run.vision, "check_connection",
                      return_value={"ok": False, "model": "m", "error": "BadRequestError: 400"}), \
         pytest.raises(run.VisionUnavailable) as caught:
        run.run_batch(batch["id"])

    assert "400" in str(caught.value), "the reason must survive to the caller"
    # Untouched: a re-run after the fix picks the ticket up exactly as it was.
    assert db.get_ticket(t["ticket_id"])["status"] == "pending_review"
    assert not db.lines_for_ticket(t["ticket_id"])


def test_offline_mode_never_preflights():
    import app.pipeline.run as run

    batch = db.create_batch()
    with patch.object(run.settings, "offline_mode", True), \
         patch.object(run.vision, "check_connection") as probe:
        run.run_batch(batch["id"])
    probe.assert_not_called()


# ---------------------------------------------------------------------------
# The route. This is the layer that shipped a dead branch: the 503 carried a
# dict `detail`, api.js only unwraps a string one, so the browser saw a bare
# "503 Service Unavailable" and the UI offered "try again" — the one piece of
# advice that cannot help with a misconfiguration.
# ---------------------------------------------------------------------------
def test_an_unreachable_reader_returns_503_with_a_readable_string_reason():
    from fastapi.testclient import TestClient

    import app.main as main
    import app.pipeline.run as run

    batch = db.create_batch()
    t = db.create_ticket({"batch_id": batch["id"], "source_filename": "MO-503.jpg",
                          "status": "pending_review"})

    with patch.object(run.settings, "vision_provider", "anthropic"), \
         patch.object(run.settings, "anthropic_api_key", "sk-test"), \
         patch.object(run.settings, "offline_mode", False), \
         patch.object(run.vision, "check_connection", return_value={
             "ok": False, "model": "m",
             "error": "BadRequestError: thinking.budget_tokens is not supported"}):
        resp = TestClient(main.app).post("/batches/run", json={"batch_id": batch["id"]})

    assert resp.status_code == 503
    detail = resp.json()["detail"]
    # A STRING, or api.js drops it and the reason never reaches anyone.
    assert isinstance(detail, str), "api.js only unwraps a string `detail`"
    assert "budget_tokens" in detail, "the reason has to survive to the browser"
    assert "nothing was processed" in detail.lower()
    # And the ticket is genuinely untouched, as the message promises.
    assert db.get_ticket(t["ticket_id"])["status"] == "pending_review"


def test_health_vision_reports_the_check():
    from fastapi.testclient import TestClient

    import app.main as main

    with patch("app.pipeline.vision.check_connection",
               return_value={"ok": False, "model": "m", "error": "nope"}):
        body = TestClient(main.app).get("/health/vision").json()
    assert body == {"ok": False, "model": "m", "error": "nope"}
