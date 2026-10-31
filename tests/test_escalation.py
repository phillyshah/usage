"""A second, then a third read — but only where the one before left a real gap.

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

**A ladder since 2.28.0: Sonnet, then Opus.** The two readers answer the same
question at different prices — Sonnet is half of Opus — so Sonnet goes first
and Opus is paid only for the cells that survived it. A month of production is
what argued for this: 251 Opus re-reads, 250 of which still ended with an empty
cell. Sonnet-then-Opus makes the expensive reader the exception rather than the
rule, without giving up a second look at anything.
"""
from unittest.mock import MagicMock, patch

from app.db import db
from app.pipeline import modelnames, vision

SONNET, OPUS = "claude-sonnet-5-5", "claude-opus-5-5"


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


def _live(**over):
    """A configured Anthropic escalation, with the Models API kept out of it."""
    base = {"vision_escalate_models": "sonnet,opus",
            "vision_escalate_model": "",
            "anthropic_api_key": "unit-test-credential",
            "offline_mode": False}
    base.update(over)
    return [patch.object(vision.settings, k, v) for k, v in base.items()]


def _with(patches):
    from contextlib import ExitStack
    stack = ExitStack()
    for p in patches:
        stack.enter_context(p)
    return stack


# ---------------------------------------------------------------------------
# Which cells are worth paying for
# ---------------------------------------------------------------------------
def test_the_fields_worth_escalating_are_the_ones_nothing_else_can_recover():
    """A blank Lot or Expiry comes back from the barcode or the Expiry Log.
    These four come from nowhere else, and the invoice is built from them."""
    assert vision.escalate_fields() == {
        "unit_price", "surgeon", "hospital", "surgery_date"}


def test_escalation_is_off_when_no_model_is_configured():
    with _with(_live(vision_escalate_models="", vision_escalate_model="")):
        ladder, why = vision.escalation_ladder()
    assert ladder == [] and "turned off" in why


def test_escalation_says_so_when_there_is_nothing_to_escalate_to():
    with _with(_live(anthropic_api_key="")):
        ladder, why = vision.escalation_ladder()
    assert ladder == [] and "ANTHROPIC_API_KEY" in why


# ---------------------------------------------------------------------------
# The ladder itself
# ---------------------------------------------------------------------------
def test_the_cheaper_reader_goes_first():
    """The whole saving: Sonnet is half Opus's price and reads most of what the
    open-weight model missed, so Opus only ever sees what survived Sonnet."""
    with _with(_live()):
        ladder, why = vision.escalation_ladder()
    assert why is None
    assert [r["model"] for r in ladder] == [SONNET, OPUS]


def test_the_expensive_reader_costs_less_per_token_than_nothing_claims():
    """Not a tautology — it is the premise the ladder order rests on, and it
    lives in the price table that the spend panel also reads."""
    assert (vision.MODEL_PRICES[SONNET][0] < vision.MODEL_PRICES[OPUS][0]
            and vision.MODEL_PRICES[SONNET][1] < vision.MODEL_PRICES[OPUS][1])


def test_a_single_configured_model_is_a_one_rung_ladder():
    """VISION_ESCALATE_MODEL is the old setting. A .env that pins it keeps
    behaving exactly as it did rather than silently gaining a second reader."""
    with _with(_live(vision_escalate_model=OPUS)):
        ladder, _ = vision.escalation_ladder()
    assert [r["model"] for r in ladder] == [OPUS]


def test_naming_one_model_twice_is_not_a_second_rung():
    with _with(_live(vision_escalate_models=f"{OPUS},{OPUS}")):
        ladder, _ = vision.escalation_ladder()
    assert [r["model"] for r in ladder] == [OPUS]


def test_a_rung_that_duplicates_the_primary_is_dropped_and_the_rest_still_runs():
    """The 2.25.1 guard refused the WHOLE escalation when the primary was the
    same model. With a ladder that was over-broad: a Sonnet primary is a reason
    to skip the Sonnet rung, not a reason to give up the Opus one."""
    with _with(_live(vision_provider="anthropic", anthropic_model=SONNET)):
        ladder, why = vision.escalation_ladder()
    assert why is None
    assert [r["model"] for r in ladder] == [OPUS]


def test_escalating_to_the_model_that_just_read_it_is_refused():
    """Paying twice to ask one model the same question. When the ladder is a
    single model and the primary is already it, there is nothing left."""
    with _with(_live(vision_provider="anthropic", anthropic_model=OPUS,
                     vision_escalate_models=OPUS)):
        ladder, why = vision.escalation_ladder()
    assert ladder == [] and "same model" in why


# ---------------------------------------------------------------------------
# Model names that do not go stale
# ---------------------------------------------------------------------------
def test_a_family_name_resolves_to_a_concrete_model():
    """"sonnet" is not a model id. Anthropic publishes no -latest alias, so the
    name has to be turned into something the API will accept."""
    with _with(_live()):
        ladder, _ = vision.escalation_ladder()
    assert all(r["model"].startswith("claude-") for r in ladder)
    assert [r["spec"] for r in ladder] == ["sonnet", "opus"]


def test_an_exact_id_is_left_alone():
    """An id in a setting is a deliberate pin and must not be 'helpfully'
    moved to a newer model."""
    assert modelnames.resolve(OPUS) == OPUS
    assert modelnames.resolve("qwen/qwen3-vl-235b-a22b-instruct") == \
        "qwen/qwen3-vl-235b-a22b-instruct"


def test_a_family_falls_back_to_a_pin_when_the_models_api_cannot_be_reached():
    """A name lookup must never be the thing that stops a batch."""
    modelnames.forget()
    with patch.object(modelnames, "_newest", return_value=None):
        assert modelnames.resolve("opus") == modelnames.PINNED["opus"]
    modelnames.forget()


def test_the_newest_model_in_the_family_wins():
    modelnames.forget()
    listed = [MagicMock(id="claude-opus-5-5", created_at="2026-04-01",
                        capabilities={"image_input": {"supported": True}}),
              MagicMock(id="claude-opus-6", created_at="2027-01-15",
                        capabilities={"image_input": {"supported": True}}),
              MagicMock(id="claude-sonnet-9", created_at="2028-01-01",
                        capabilities={"image_input": {"supported": True}})]
    client = MagicMock()
    client.models.list.return_value = listed
    import anthropic
    with patch.object(vision.settings, "anthropic_api_key", "k"), \
         patch.object(anthropic, "Anthropic", return_value=client):
        assert modelnames.resolve("opus") == "claude-opus-6"
    modelnames.forget()


def test_a_model_that_cannot_see_an_image_is_never_chosen():
    """These are photographs. A text-only model would fail every ticket in the
    batch, and it would be the newest thing in the family that did it."""
    modelnames.forget()
    listed = [MagicMock(id="claude-opus-5-5", created_at="2026-04-01",
                        capabilities={"image_input": {"supported": True}}),
              MagicMock(id="claude-opus-7-text", created_at="2027-06-01",
                        capabilities={"image_input": {"supported": False}})]
    client = MagicMock()
    client.models.list.return_value = listed
    import anthropic
    with patch.object(vision.settings, "anthropic_api_key", "k"), \
         patch.object(anthropic, "Anthropic", return_value=client):
        assert modelnames.resolve("opus") == "claude-opus-5-5"
    modelnames.forget()


def test_the_lookup_is_not_repeated_per_ticket():
    modelnames.forget()
    with patch.object(modelnames, "_newest", return_value="claude-opus-9") as nm:
        [modelnames.resolve("opus") for _ in range(5)]
    assert nm.call_count == 1
    modelnames.forget()


def test_the_configured_default_tracks_the_family_rather_than_a_version():
    from app.config import Settings

    cfg = Settings(_env_file=None)
    assert cfg.vision_escalate_models == "sonnet,opus"
    assert cfg.anthropic_model == "sonnet"


# ---------------------------------------------------------------------------
# Merging the reads
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


def test_three_reads_merge_without_losing_the_middle_one():
    """The ladder merges twice. What Sonnet contributed has to survive the
    second merge, or the Opus rung would silently discard it."""
    primary = _read(hospital="Enloe")
    sonnet = _read(surgeon="Konkin")
    opus = _read(price=1300)
    merged = vision.merge_reads(vision.merge_reads(primary, sonnet), opus)
    assert merged["header"]["hospital"]["value"] == "Enloe"
    assert merged["header"]["surgeon"]["value"] == "Konkin"
    assert merged["lines"][0]["unit_price"]["value"] == 1300


def test_every_read_in_the_ladder_is_accounted_for():
    """Three calls were paid for. If the merge kept only the last one's usage,
    the ladder would look cheaper than a single Opus read."""
    def u(model):
        return {**_read(surgeon="K"),
                "usage": [{"model": model, "tokens_in": 1, "tokens_out": 1}]}

    merged = vision.merge_reads(vision.merge_reads(u("qwen"), u(SONNET)), u(OPUS))
    assert [r["model"] for r in merged["usage"]] == ["qwen", SONNET, OPUS]


# ---------------------------------------------------------------------------
# The trigger, end to end through process_ticket
# ---------------------------------------------------------------------------
def _ticket():
    batch = db.create_batch()
    t = db.create_ticket({"batch_id": batch["id"], "source_filename": "MO-esc.jpg",
                          "status": "pending_review"})
    return db.get_ticket(t["ticket_id"])


def _run(first, *escalated, ladder=(SONNET, OPUS)):
    """process_ticket with a stubbed reader.

    Returns (summary, call_log) where the log names which model each call went
    to, so the tests can assert that a rung was NOT paid for.
    """
    import app.pipeline.run as run

    ticket = _ticket()
    calls = []
    queue = list(escalated)
    rungs = [{"provider": "anthropic", "model": m, "spec": m} for m in ladder]

    def fake_extract(img, media_type="image/jpeg", escalate=False):
        if not escalate:
            calls.append("primary")
            return first
        calls.append(escalate["model"] if isinstance(escalate, dict) else "escalated")
        return queue.pop(0) if queue else first

    with patch.object(run.vision, "extract_handwritten", side_effect=fake_extract), \
         patch.object(run.vision, "escalation_ladder",
                      return_value=(rungs, None) if rungs else ([], "off")), \
         patch.object(run, "_grid_crop", return_value=None), \
         patch.object(run.barcode, "decode_region", return_value=[]):
        summary = run.process_ticket(ticket)
    return summary, calls


def _flags(ticket_id):
    return " ".join(str(f) for f in db.get_ticket(ticket_id)["flags"])


def test_a_ticket_with_everything_filled_is_not_escalated():
    """The whole saving depends on this: a good read must cost one call."""
    summary, calls = _run(_read(surgeon="Konkin", hospital="Enloe",
                                date="2026-09-28", price=1300))
    assert calls == ["primary"], "a complete ticket must not pay for a second read"
    assert "escalated_to" not in summary


def test_a_gap_goes_to_the_cheaper_reader_first():
    summary, calls = _run(
        _read(surgeon="Konkin", hospital="Enloe", date="2026-09-28", price=None),
        _read(surgeon="Konkin", hospital="Enloe", date="2026-09-28", price=1300))
    assert calls == ["primary", SONNET], "Opus must not be paid for what Sonnet read"
    assert summary["escalated_to"] == SONNET


def test_what_the_cheaper_reader_also_misses_goes_on_to_opus():
    """The point of the third rung: still red after Sonnet is the only state
    that has ever justified Opus money."""
    gap = _read(surgeon="Konkin", hospital="Enloe", date="2026-09-28", price=None)
    summary, calls = _run(
        gap,                                                     # primary: no price
        gap,                                                     # Sonnet: still none
        _read(surgeon="Konkin", hospital="Enloe", date="2026-09-28", price=1300))
    assert calls == ["primary", SONNET, OPUS]
    assert summary["escalated_to"] == OPUS
    assert db.lines_for_ticket(summary["ticket_id"])[0]["unit_price"] == 1300


def test_the_second_read_reaches_the_stored_ticket():
    summary, _ = _run(
        _read(surgeon=None, hospital="Enloe", date="2026-09-28", price=1300),
        _read(surgeon="Konkin", hospital="Enloe", date="2026-09-28", price=1300))
    assert db.get_ticket(summary["ticket_id"])["surgeon"] == "Konkin"


def test_the_ticket_says_which_model_re_read_it_and_why():
    """Nobody should have to wonder which model produced a value — and with two
    possible re-readers that question now has two answers."""
    summary, _ = _run(
        _read(surgeon=None, hospital="Enloe", date="2026-09-28", price=1300),
        _read(surgeon="Konkin", hospital="Enloe", date="2026-09-28", price=1300))
    flags = _flags(summary["ticket_id"])
    assert f"Re-read by {SONNET}" in flags
    assert "surgeon" in flags


def test_each_rung_is_recorded_separately():
    gap = _read(hospital="Enloe", date="2026-09-28", price=1300)   # no surgeon
    summary, _ = _run(gap, gap,
                      _read(surgeon="Konkin", hospital="Enloe",
                            date="2026-09-28", price=1300))
    flags = _flags(summary["ticket_id"])
    assert f"Re-read by {SONNET}" in flags and f"Re-read by {OPUS}" in flags
    assert "still empty" in flags          # what Sonnet left
    assert "filled surgeon" in flags       # what Opus got


def test_a_gap_no_reader_fills_is_reported_not_hidden():
    gap = _read(hospital="Enloe", date="2026-09-28", price=1300)
    summary, calls = _run(gap, gap, gap)
    assert calls == ["primary", SONNET, OPUS]
    assert "still empty" in _flags(summary["ticket_id"])


def test_a_failed_rung_leaves_the_read_standing_and_tries_the_next_one():
    """An escalation that errors must not destroy a usable read, and must not
    cost the ticket its remaining chance at the gap."""
    broken = {"header": {}, "lines": [], "freight": _f(None, "low"),
              "grand_total": _f(None, "low"), "error": "APIStatusError: 500"}
    summary, calls = _run(
        _read(surgeon="Konkin", hospital=None, date="2026-09-28", price=1300),
        broken,
        _read(surgeon="Konkin", hospital="Enloe", date="2026-09-28", price=1300))
    assert calls == ["primary", SONNET, OPUS]
    ticket = db.get_ticket(summary["ticket_id"])
    assert ticket["surgeon"] == "Konkin"      # the primary read survived
    assert ticket["hospital"] == "Enloe"      # and Opus filled the gap
    assert "failed" in _flags(summary["ticket_id"])


def test_nothing_is_escalated_when_escalation_is_unavailable():
    summary, calls = _run(
        _read(surgeon=None, hospital="Enloe", date="2026-09-28", price=1300),
        ladder=())
    assert calls == ["primary"]


# ---------------------------------------------------------------------------
# The preflight covers the readers it cannot see the names of in advance
# ---------------------------------------------------------------------------
def test_the_preflight_probes_the_second_readers_too():
    """A rung can name a family, so which model answers it is decided at run
    time. The alternative to checking here is finding out when a ticket
    escalates in the middle of a batch."""
    with _with(_live(vision_provider="anthropic", anthropic_model="qwen-ish")), \
         patch.object(vision, "_TRANSPORTS",
                      {"anthropic": MagicMock(return_value=("", None, {}))}):
        out = vision.check_connection()
    assert out["ok"]
    assert [r["model"] for r in out["escalation"]] == [SONNET, OPUS]
    assert all(r["ok"] for r in out["escalation"])


def test_an_unreachable_second_reader_does_not_fail_the_preflight():
    """Escalation improves on the primary read; it is not required for one. A
    batch that can still be read must not be stopped."""
    def transport(img, media_type, probe=False, model=None):
        if model:                      # a rung, not the primary
            raise RuntimeError("404 model not found")
        return "", None, {}

    with _with(_live(vision_provider="anthropic", anthropic_model="qwen-ish")), \
         patch.object(vision, "_TRANSPORTS", {"anthropic": transport}):
        out = vision.check_connection()
    assert out["ok"] is True
    assert all(r["ok"] is False for r in out["escalation"])
    assert "404 model not found" in out["escalation"][0]["error"]
