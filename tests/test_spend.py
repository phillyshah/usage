"""What the reading costs (app/metrics.vision_spend).

Two spend caps stopped a batch in three weeks — one on each provider — and both
times the first anybody knew was a failed run. The token counts were in every
API response the whole time and were being dropped on the floor, so the only
place that could answer "where is the money going" was a provider's console,
after the fact.

These tests pin the two things that make the number trustworthy: both reads of
an escalated ticket are counted, and a model with no published price reports
its tokens and NO dollars rather than a confident wrong total.
"""
import json
from unittest.mock import patch

from app.db import db
from app.metrics import vision_spend
from app.pipeline import vision
from app.pipeline.assemble import assemble_and_persist


def _f(value, confidence="high"):
    return {"value": value, "confidence": confidence}


def _usage(model, tin, tout, escalated=False):
    return {"provider": "openrouter", "model": model, "escalated": escalated,
            "tokens_in": tin, "tokens_out": tout,
            "usd": vision.estimate_cost(model, tin, tout)}


def _read(usage):
    return {"header": {"surgeon": _f("Konkin")}, "lines": [],
            "freight": _f(None, "low"), "grand_total": _f(None, "low"),
            "usage": usage}


def _persist(usage, filename="MO-spend.jpg"):
    batch = db.create_batch()
    t = db.create_ticket({"batch_id": batch["id"], "source_filename": filename,
                          "status": "pending_review"})
    assemble_and_persist(db.get_ticket(t["ticket_id"]), _read(usage), [])
    return t["ticket_id"]


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------
def test_a_known_model_is_priced():
    cost = vision.estimate_cost("qwen/qwen3-vl-235b-a22b-instruct", 1_000_000, 0)
    assert cost == 0.20


def test_output_tokens_cost_more_than_input():
    model = "claude-opus-5-5"
    assert (vision.estimate_cost(model, 0, 1_000_000)
            > vision.estimate_cost(model, 1_000_000, 0))


def test_an_unknown_model_reports_no_price_rather_than_zero():
    """A zero would quietly understate a bill, which is the exact failure this
    whole feature exists to prevent."""
    assert vision.estimate_cost("some/model-nobody-priced", 10_000, 1_000) is None


def test_missing_token_counts_are_not_guessed_at():
    assert vision.estimate_cost("claude-opus-5-5", None, None) is None


# ---------------------------------------------------------------------------
# Recording, without a migration
# ---------------------------------------------------------------------------
def test_the_cost_of_a_read_is_recorded_on_the_ticket():
    tid = _persist([_usage("qwen/qwen3-vl-235b-a22b-instruct", 20_000, 2_000)])
    rows = [r for r in db.field_extractions_for_ticket(tid)
            if r.get("field_name") == "vision_usage"]
    assert len(rows) == 1
    assert json.loads(rows[0]["orig_value"])[0]["tokens_in"] == 20_000


def test_the_usage_row_is_not_mistaken_for_a_field():
    """It rides in field_extractions to avoid a migration, so everything that
    walks those rows has to skip it — as it already skips raw_blob."""
    from app.pipeline.assemble import confidence_map_for_ticket

    tid = _persist([_usage("qwen/qwen3-vl-235b-a22b-instruct", 10, 10)])
    cmap = confidence_map_for_ticket(tid)
    assert "vision_usage" not in (cmap.get("header") or {})


# ---------------------------------------------------------------------------
# The aggregate
# ---------------------------------------------------------------------------
def test_spend_adds_up_across_tickets():
    before = vision_spend(14)["total_usd"]
    _persist([_usage("claude-opus-5-5", 1_000_000, 0)], "MO-a.jpg")
    _persist([_usage("claude-opus-5-5", 1_000_000, 0)], "MO-b.jpg")
    after = vision_spend(14)
    assert round(after["total_usd"] - before, 2) == 8.00   # 2 x $4.00


def test_both_reads_of_an_escalated_ticket_are_counted():
    """Escalation would look free if only the last read were recorded."""
    before = vision_spend(14)
    _persist([_usage("qwen/qwen3-vl-235b-a22b-instruct", 1_000_000, 0),
              _usage("claude-opus-5-5", 1_000_000, 0, escalated=True)],
             "MO-esc-spend.jpg")
    after = vision_spend(14)
    assert round(after["total_usd"] - before["total_usd"], 2) == 4.20  # 0.20 + 4.00
    assert after["escalated_tickets"] == before["escalated_tickets"] + 1


def test_an_unpriced_model_is_named_rather_than_silently_dropped():
    _persist([_usage("some/model-nobody-priced", 50_000, 5_000)], "MO-unpriced.jpg")
    out = vision_spend(14)
    assert "some/model-nobody-priced" in out["unpriced_models"]
    assert out["by_model"]["some/model-nobody-priced"]["priced"] is False
    # Its tokens still count, so the usage is visible even when the cost isn't.
    assert out["by_model"]["some/model-nobody-priced"]["tokens_in"] == 50_000


def test_the_month_projection_scales_the_observed_rate():
    out = vision_spend(14)
    if out["total_usd"]:
        assert out["projected_month_usd"] == round(out["total_usd"] / 14 * 30, 2)


def test_no_reads_is_not_an_error():
    out = vision_spend(0)
    assert out["total_usd"] == 0 and out["tickets"] == 0


# ---------------------------------------------------------------------------
# It reaches the people who need it
# ---------------------------------------------------------------------------
def test_the_status_email_carries_the_month_to_date_figure():
    from app.notify import DayStatus, render

    status = DayStatus("2026-10-19", tickets_uploaded=40, batches_generated=2,
                       tickets_pending=5, tickets_verified=35, price_runs=1,
                       consecutive_misses=0, spend_mtd_usd=25.03,
                       spend_projected_usd=39.52)
    _, text, _ = render(status)
    assert "$25.03" in text and "$39.52" in text


def test_a_failure_to_price_never_sinks_the_status_email():
    """Losing the daily status because a cost estimate failed would trade
    something people rely on for something they merely like."""
    from app import notify

    with patch("app.metrics.vision_spend", side_effect=RuntimeError("boom")):
        fields = notify._spend_fields()
    assert fields == {}
