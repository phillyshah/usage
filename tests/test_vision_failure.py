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


def _live(client):
    """Patch enough that extract_handwritten takes the real API path."""
    import anthropic
    return (
        patch.object(vision.settings, "anthropic_api_key", "sk-test"),
        patch.object(vision.settings, "offline_mode", False),
        patch.object(anthropic, "Anthropic", return_value=client),
    )


def _extract(client):
    a, b, c = _live(client)
    with a, b, c:
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

    a, b, c = _live(_client(raises=RateLimitError("slow down")))
    with a, b, c, pytest.raises(RateLimitError):
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
