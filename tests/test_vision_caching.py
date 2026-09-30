"""Prompt caching on the vision extraction call (app/pipeline/vision.py).

SYSTEM_PROMPT is static and sent on every ticket extraction — this locks in
that it's passed as a cache_control-annotated content block (not a bare
string), so Anthropic can cache it across calls instead of reprocessing the
~1000+ token prompt on every ticket.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.pipeline import vision


def _fake_response(cache_read=0, cache_write=0):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text='{"header": {}, "lines": [], "freight": null, "grand_total": null}')],
        usage=SimpleNamespace(
            input_tokens=50, output_tokens=20,
            cache_read_input_tokens=cache_read, cache_creation_input_tokens=cache_write,
        ),
    )


def test_system_prompt_sent_as_cache_control_block():
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _fake_response()

    with patch.object(vision.settings, "vision_provider", "anthropic"), \
         patch.object(vision.settings, "anthropic_api_key", "sk-test"), \
         patch.object(vision.settings, "offline_mode", False), \
         patch("anthropic.Anthropic", return_value=fake_client):
        vision.extract_handwritten(b"fake-jpeg-bytes")

    call_kwargs = fake_client.messages.create.call_args.kwargs
    system = call_kwargs["system"]
    assert isinstance(system, list), "system must be a content-block list, not a bare string, to carry cache_control"
    assert system[0]["type"] == "text"
    assert system[0]["text"] == vision.SYSTEM_PROMPT
    assert system[0]["cache_control"] == {"type": "ephemeral"}


def _request_kwargs() -> dict:
    """The kwargs extract_handwritten actually hands to messages.create."""
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _fake_response()

    with patch.object(vision.settings, "vision_provider", "anthropic"), \
         patch.object(vision.settings, "anthropic_api_key", "sk-test"), \
         patch.object(vision.settings, "offline_mode", False), \
         patch("anthropic.Anthropic", return_value=fake_client):
        vision.extract_handwritten(b"fake-jpeg-bytes")

    return fake_client.messages.create.call_args.kwargs


# ---------------------------------------------------------------------------
# The request SHAPE. These exist because a whole release shipped a call that
# was rejected with a 400 on every single ticket while 382 tests passed: a
# mocked client accepts any keyword argument ever invented, so nothing here can
# validate the contract. Only vision.check_connection(), against the real API,
# can do that. What these CAN do is pin the shape we mean to send, and that is
# enough to have caught the bug that cost a batch.
# ---------------------------------------------------------------------------
def test_thinking_is_adaptive_and_carries_no_token_budget():
    """`{"type": "enabled", "budget_tokens": N}` is rejected outright by this
    model family. Adaptive is the only on-mode; depth comes from effort."""
    thinking = _request_kwargs()["thinking"]
    assert thinking == {"type": "adaptive"}


def test_no_token_budget_anywhere_in_the_request():
    """Belt and braces: the 400 was worth a batch, so don't rely on one key."""
    import json

    assert "budget_tokens" not in json.dumps(_request_kwargs(), default=str)


def test_extraction_runs_at_high_effort():
    """Sonnet 5.5 recalibrated the effort levels, so the "medium" tuned against
    Sonnet 5 no longer means what it did. Pinned so it can't drift silently."""
    assert _request_kwargs()["output_config"] == {"effort": "high"}


def test_max_tokens_leaves_room_for_the_answer():
    """max_tokens covers thinking AND output. An 8000 cap is the best-supported
    explanation for the batch that came back with nothing."""
    assert _request_kwargs()["max_tokens"] >= 16000


def test_cache_stats_recorded_in_trace():
    from app.pipeline import tracer

    fake_client = MagicMock()
    fake_client.messages.create.return_value = _fake_response(cache_read=1234, cache_write=0)
    # The Anthropic transport specifically — OpenRouter is the default now and
    # has no prompt cache to report.

    steps = tracer.start()
    try:
        with patch.object(vision.settings, "vision_provider", "anthropic"), \
             patch.object(vision.settings, "anthropic_api_key", "sk-test"), \
             patch.object(vision.settings, "offline_mode", False), \
             patch("anthropic.Anthropic", return_value=fake_client):
            vision.extract_handwritten(b"fake-jpeg-bytes")
    finally:
        tracer.stop()

    vision_step = next(s for s in steps if s["stage"] == "vision_ai")
    assert vision_step["detail"]["cache_read_input_tokens"] == 1234
    assert "cached" in vision_step["summary"]
