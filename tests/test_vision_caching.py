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

    with patch.object(vision.settings, "anthropic_api_key", "sk-test"), \
         patch.object(vision.settings, "offline_mode", False), \
         patch("anthropic.Anthropic", return_value=fake_client):
        vision.extract_handwritten(b"fake-jpeg-bytes")

    call_kwargs = fake_client.messages.create.call_args.kwargs
    system = call_kwargs["system"]
    assert isinstance(system, list), "system must be a content-block list, not a bare string, to carry cache_control"
    assert system[0]["type"] == "text"
    assert system[0]["text"] == vision.SYSTEM_PROMPT
    assert system[0]["cache_control"] == {"type": "ephemeral"}


def test_cache_stats_recorded_in_trace():
    from app.pipeline import tracer

    fake_client = MagicMock()
    fake_client.messages.create.return_value = _fake_response(cache_read=1234, cache_write=0)

    steps = tracer.start()
    try:
        with patch.object(vision.settings, "anthropic_api_key", "sk-test"), \
             patch.object(vision.settings, "offline_mode", False), \
             patch("anthropic.Anthropic", return_value=fake_client):
            vision.extract_handwritten(b"fake-jpeg-bytes")
    finally:
        tracer.stop()

    vision_step = next(s for s in steps if s["stage"] == "vision_ai")
    assert vision_step["detail"]["cache_read_input_tokens"] == 1234
    assert "cached" in vision_step["summary"]
