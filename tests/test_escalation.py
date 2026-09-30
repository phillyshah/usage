"""A second, stronger read — but only where the first one left a real gap.

Andy's idea, and the economics are the point: re-reading every ticket with
Claude costs more than never having switched provider, and re-reading none
leaves the cheap model's misses in the deliverable. Escalating only the tickets
with an unfilled cell makes the bill track the failure rate instead of the
volume — cheaper than Claude-only AND more accurate than open-weight-only.

One correction to the original idea, which these tests encode: you cannot
cheaply re-read a single CELL. The cost of a vision call is dominated by
re-sending the image, so the unit is the ticket.

The trigger is RED, not amber. Amber means the tool has a candidate and a human
confirms it, which is already cheap. Red means nobody has anything — the only
state a second reader can improve on.
"""
from unittest.mock import MagicMock, patch

from app.db import db
from app.pipeline import vision


def _f(value, confidence="high"):
    return {"value": value, "confidence": confidence}


def _read(surgeon=None, hospital=None, date=None, price=None, ref="ESC-REF-1"):
    return {
        "header": {"surgeon": _f(surgeon, "high" if surgeon else "low"),
                   "hospital": _f(hospital, "high" if hospital else "low"),
                   "surgery_date": _f(date, "high" if date else "low")},
        "lines": [{"index": 0, "ref": _f(ref), "qty": _f(1),
                   "unit_price": _f(price, "high" if price else "low")}],
        "freight": _f(None, "low"),
        "grand_total": _f(None, "low"),
    }


# ---------------------------------------------------------------------------
# Which cells are worth paying for
# ---------------------------------------------------------------------------
def test_the_fields_worth_escalating_are_the_ones_nothing_else_can_recover():
    """A blank Lot or Expiry comes back from the barcode or the Expiry Log.
    These four come from nowhere else, and the invoice is built from them."""
    assert vision.escalate_fields() == {
        "unit_price", "surgeon", "hospital", "surgery_date"}


def test_escalation_is_off_when_no_model_is_configured():
    with patch.object(vision.settings, "vision_escalate_model", ""):
        cfg, why = vision.escalation_configuration()
    assert cfg is None and "turned off" in why


def test_escalation_says_so_when_there_is_nothing_to_escalate_to():
    with patch.object(vision.settings, "vision_escalate_model", "claude-opus-5-5"), \
         patch.object(vision.settings, "offline_mode", False), \
         patch.object(vision.settings, "anthropic_api_key", ""):
        cfg, why = vision.escalation_configuration()
    assert cfg is None and "ANTHROPIC_API_KEY" in why


def test_the_escalation_model_is_the_strong_one():
    from app.config import Settings

    assert "opus" in Settings(_env_file=None).vision_escalate_model


# ---------------------------------------------------------------------------
# Merging the two reads
# ---------------------------------------------------------------------------
def test_the_stronger_read_wins_where_both_read_something():
    merged = vision.merge_reads(_read(surgeon="Kronkin"), _read(surgeon="Konkin"))
    assert merged["header"]["surgeon"]["value"] == "Konkin"


def test_the_first_read_fills_what_the_second_one_missed():
    """The merge must never be WORSE than either read alone — a field the cheap
    model got right should not regress because the strong one happened to miss
    it."""
    primary = _read(surgeon="Konkin", hospital="Enloe")
    escalated = _read(surgeon="Konkin")  # no hospital
    merged = vision.merge_reads(primary, escalated)
    assert merged["header"]["hospital"]["value"] == "Enloe"


def test_line_prices_merge_per_field():
    merged = vision.merge_reads(_read(price=None), _read(price=1300))
    assert merged["lines"][0]["unit_price"]["value"] == 1300


def test_a_line_only_the_second_read_found_is_kept():
    primary = {"header": {}, "lines": [], "freight": _f(None, "low"),
               "grand_total": _f(None, "low")}
    escalated = _read(price=250)
    assert len(vision.merge_reads(primary, escalated)["lines"]) == 1


def test_the_merged_read_carries_no_error():
    """Either read may have had one; the merge is a fresh, usable result."""
    merged = vision.merge_reads(_read(surgeon="A"), _read(surgeon="B"))
    assert merged.get("error") is None


# ---------------------------------------------------------------------------
# The trigger, end to end through process_ticket
# ---------------------------------------------------------------------------
def _ticket():
    batch = db.create_batch()
    t = db.create_ticket({"batch_id": batch["id"], "source_filename": "MO-esc.jpg",
                          "status": "pending_review"})
    return db.get_ticket(t["ticket_id"])


def _run(first, second=None, escalate_model="claude-opus-5-5"):
    """process_ticket with a stubbed reader; returns (summary, call_log)."""
    import app.pipeline.run as run

    ticket = _ticket()
    calls = []

    def fake_extract(img, media_type="image/jpeg", escalate=False):
        calls.append("escalated" if escalate else "primary")
        return second if escalate else first

    with patch.object(run.vision, "extract_handwritten", side_effect=fake_extract), \
         patch.object(run.vision, "escalation_configuration",
                      return_value=(({"provider": "anthropic",
                                      "model": escalate_model}, None)
                                    if escalate_model else (None, "off"))), \
         patch.object(run, "_grid_crop", return_value=None), \
         patch.object(run.barcode, "decode_region", return_value=[]):
        summary = run.process_ticket(ticket)
    return summary, calls


def test_a_ticket_with_everything_filled_is_not_escalated():
    """The whole saving depends on this: a good read must cost one call."""
    summary, calls = _run(_read(surgeon="Konkin", hospital="Enloe",
                                date="2026-09-28", price=1300))
    assert calls == ["primary"], "a complete ticket must not pay for a second read"
    assert "escalated_to" not in summary


def test_a_ticket_with_an_unread_price_is_escalated():
    summary, calls = _run(
        _read(surgeon="Konkin", hospital="Enloe", date="2026-09-28", price=None),
        _read(surgeon="Konkin", hospital="Enloe", date="2026-09-28", price=1300))
    assert calls == ["primary", "escalated"]
    assert summary["escalated_to"] == "claude-opus-5-5"


def test_the_second_read_reaches_the_stored_ticket():
    summary, _ = _run(
        _read(surgeon=None, hospital="Enloe", date="2026-09-28", price=1300),
        _read(surgeon="Konkin", hospital="Enloe", date="2026-09-28", price=1300))
    assert db.get_ticket(summary["ticket_id"])["surgeon"] == "Konkin"


def test_the_ticket_says_it_was_re_read_and_why():
    """Nobody should have to wonder which model produced a value."""
    summary, _ = _run(
        _read(surgeon=None, hospital="Enloe", date="2026-09-28", price=1300),
        _read(surgeon="Konkin", hospital="Enloe", date="2026-09-28", price=1300))
    flags = " ".join(str(f) for f in db.get_ticket(summary["ticket_id"])["flags"])
    assert "Re-read by claude-opus-5-5" in flags
    assert "surgeon" in flags


def test_a_gap_the_second_read_also_misses_is_reported_not_hidden():
    summary, _ = _run(
        _read(hospital="Enloe", date="2026-09-28", price=1300),
        _read(hospital="Enloe", date="2026-09-28", price=1300))
    flags = " ".join(str(f) for f in db.get_ticket(summary["ticket_id"])["flags"])
    assert "still empty" in flags


def test_a_failed_second_read_leaves_the_first_one_standing():
    """An escalation that errors must not destroy a usable primary read."""
    summary, calls = _run(
        _read(surgeon="Konkin", hospital="Enloe", date="2026-09-28", price=None),
        {"header": {}, "lines": [], "freight": _f(None, "low"),
         "grand_total": _f(None, "low"), "error": "APIStatusError: 500"})
    assert calls == ["primary", "escalated"]
    assert db.get_ticket(summary["ticket_id"])["surgeon"] == "Konkin"


def test_nothing_is_escalated_when_escalation_is_unavailable():
    summary, calls = _run(
        _read(surgeon="Konkin", hospital="Enloe", date="2026-09-28", price=None),
        None, escalate_model=None)
    assert calls == ["primary"]
