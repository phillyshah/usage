"""Two providers, one pipeline (app/pipeline/vision.py).

VISION_PROVIDER picks Anthropic (the default) or OpenRouter, for open-weight
models at roughly a tenth of the cost. Only the transport differs. Everything
that took two outages to get right — the error marker, the truncation check,
the retry classification — is shared, and these tests exist to keep it that
way: a second copy of that logic would be a second place for a failure to go
quiet.
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.pipeline import vision

FAKE_KEY = "unit-test-credential"

GOOD = json.dumps({
    "header": {"surgeon": {"value": "Konkin", "confidence": "high"}},
    "lines": [{"index": 0, "ref": {"value": "MO-STOC-32/11", "confidence": "high"},
               "unit_price": {"value": 1300, "confidence": "high"}}],
    "freight": {"value": None, "confidence": "low"},
    "grand_total": {"value": 2550, "confidence": "high"},
})


def _chat_response(text=GOOD, finish_reason="stop", model="qwen/served-me"):
    """An OpenAI-compatible chat completion, which is what OpenRouter returns."""
    return SimpleNamespace(
        choices=[SimpleNamespace(
            finish_reason=finish_reason,
            message=SimpleNamespace(content=text),
        )],
        usage=SimpleNamespace(prompt_tokens=1200, completion_tokens=300),
        model=model,
    )


def _client(response=None, raises=None):
    c = MagicMock()
    if raises is not None:
        c.chat.completions.create.side_effect = raises
    else:
        c.chat.completions.create.return_value = response or _chat_response()
    return c


class _Router:
    """Context manager: VISION_PROVIDER=openrouter with a fake client."""

    def __init__(self, client, **over):
        self.client = client
        self.over = {"vision_provider": "openrouter",
                     "openrouter_api_key": FAKE_KEY,
                     "offline_mode": False, **over}
        self._ctxs = []

    def __enter__(self):
        import openai
        for k, v in self.over.items():
            c = patch.object(vision.settings, k, v)
            c.start()
            self._ctxs.append(c)
        c = patch.object(openai, "OpenAI", return_value=self.client)
        c.start()
        self._ctxs.append(c)
        return self.client

    def __exit__(self, *exc):
        for c in reversed(self._ctxs):
            c.stop()
        return False


# ---------------------------------------------------------------------------
# Configuration: a reason, not an exception, and the right variable names
# ---------------------------------------------------------------------------
def test_the_default_provider_is_anthropic():
    """An existing deployment that sets nothing must behave exactly as it did."""
    from app.config import Settings

    assert Settings(_env_file=None).vision_provider == "anthropic"


def test_an_unknown_provider_falls_back_to_anthropic():
    with patch.object(vision.settings, "vision_provider", "wat"):
        assert vision._provider() == "anthropic"


def test_openrouter_without_a_key_names_the_variable_that_is_missing():
    """Telling an administrator to check ANTHROPIC_MODEL on a box running
    OpenRouter sends them to a setting that does not exist."""
    with patch.object(vision.settings, "vision_provider", "openrouter"), \
         patch.object(vision.settings, "openrouter_api_key", ""), \
         patch.object(vision.settings, "offline_mode", False):
        cfg, reason = vision.vision_configuration()
    assert cfg is None
    assert "OPENROUTER_API_KEY" in reason


def test_configuration_reports_the_provider_and_its_model():
    with patch.object(vision.settings, "vision_provider", "openrouter"), \
         patch.object(vision.settings, "openrouter_api_key", FAKE_KEY), \
         patch.object(vision.settings, "offline_mode", False):
        cfg, reason = vision.vision_configuration()
    assert reason is None
    assert cfg["provider"] == "openrouter"
    assert cfg["key_variable"] == "OPENROUTER_API_KEY"
    assert "qwen" in cfg["model"]


# ---------------------------------------------------------------------------
# The model list the router walks
# ---------------------------------------------------------------------------
def test_the_fallback_follows_the_primary():
    with patch.object(vision.settings, "openrouter_model", "a/one"), \
         patch.object(vision.settings, "openrouter_fallback_model", "b/two"):
        assert vision._openrouter_models() == ["a/one", "b/two"]


def test_naming_one_model_twice_is_not_a_fallback():
    """Asking the router to retry the thing that just failed is not a backup."""
    with patch.object(vision.settings, "openrouter_model", "a/one"), \
         patch.object(vision.settings, "openrouter_fallback_model", "a/one"):
        assert vision._openrouter_models() == ["a/one"]


def test_the_fallback_can_be_turned_off_by_a_setting():
    """A backup that cannot be removed is a model somebody pays for unseen."""
    with patch.object(vision.settings, "openrouter_model", "a/one"), \
         patch.object(vision.settings, "openrouter_fallback_model", "none"):
        assert vision._openrouter_models() == ["a/one"]


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------
def test_the_image_is_sent_as_a_data_url():
    client = _client()
    with _Router(client):
        vision.extract_handwritten(b"jpegbytes", "image/jpeg")
    content = client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
    image = next(b for b in content if b["type"] == "image_url")
    assert image["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_every_request_denies_training_on_our_tickets():
    """Left unset, OpenRouter's provider routing defaults to allowing data
    collection. The safe answer is only true if it is sent."""
    client = _client()
    with _Router(client):
        vision.extract_handwritten(b"jpegbytes")
    body = client.chat.completions.create.call_args.kwargs["extra_body"]
    assert body["provider"] == {"data_collection": "deny"}


def test_the_fallback_model_travels_with_the_request():
    client = _client()
    with _Router(client):
        vision.extract_handwritten(b"jpegbytes")
    body = client.chat.completions.create.call_args.kwargs["extra_body"]
    assert len(body["models"]) == 2, "one request, not a second retry loop of ours"


def test_the_system_prompt_is_the_same_one_anthropic_gets():
    """Two prompts would be two things to keep correct."""
    client = _client()
    with _Router(client):
        vision.extract_handwritten(b"jpegbytes")
    msgs = client.chat.completions.create.call_args.kwargs["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == vision.SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# The answer — and the shared failure handling
# ---------------------------------------------------------------------------
def test_a_good_answer_is_parsed_the_same_way():
    with _Router(_client()):
        result = vision.extract_handwritten(b"jpegbytes")
    assert not result.get("error")
    assert result["header"]["surgeon"]["value"] == "Konkin"
    assert result["grand_total"]["value"] == 2550


def test_a_truncated_answer_is_reported_as_truncated():
    """OpenAI-compatible "length" means the same thing Anthropic calls
    "max_tokens" — one vocabulary, so one truncation check."""
    with _Router(_client(_chat_response(text='{"header": {"surg',
                                        finish_reason="length"))):
        result = vision.extract_handwritten(b"jpegbytes")
    assert result["error"] and "truncated" in result["error"]


def test_a_filtered_answer_is_reported_as_a_refusal():
    with _Router(_client(_chat_response(text="", finish_reason="content_filter"))):
        result = vision.extract_handwritten(b"jpegbytes")
    assert result["error"] and "declined" in result["error"]


def test_prose_instead_of_json_is_reported_not_swallowed():
    with _Router(_client(_chat_response(text="Sure! Here are the fields:"))):
        result = vision.extract_handwritten(b"jpegbytes")
    assert result["error"] and "unparseable" in result["error"]


def test_an_api_error_is_reported_not_swallowed():
    with _Router(_client(raises=Exception("openrouter is unhappy"))):
        result = vision.extract_handwritten(b"jpegbytes")
    assert result["error"] and "openrouter is unhappy" in result["error"]


def test_a_transient_failure_is_still_raised_for_the_retry_loop():
    class RateLimitError(Exception):
        pass

    with _Router(_client(raises=RateLimitError("slow down"))), \
         pytest.raises(RateLimitError):
        vision.extract_handwritten(b"jpegbytes")


def test_the_model_that_actually_answered_is_recorded():
    """The router may have walked to the backup, and a quality question is
    unanswerable without knowing which model produced the reading."""
    from app.pipeline import tracer

    steps = tracer.start()
    try:
        with _Router(_client(_chat_response(model="qwen/the-backup"))):
            vision.extract_handwritten(b"jpegbytes")
    finally:
        tracer.stop()
    step = next(s for s in steps if s["stage"] == "vision_ai")
    assert step["detail"]["served_by"] == "qwen/the-backup"
    assert step["detail"]["provider"] == "openrouter"


# ---------------------------------------------------------------------------
# The preflight works for whichever provider is configured
# ---------------------------------------------------------------------------
def test_the_preflight_probes_openrouter_too():
    client = _client()
    with _Router(client):
        result = vision.check_connection()
    assert result["ok"] and result["provider"] == "openrouter"
    client.chat.completions.create.assert_called_once()


def test_a_broken_openrouter_config_fails_the_preflight_with_the_reason():
    with _Router(_client(raises=Exception("401 no credit"))):
        result = vision.check_connection()
    assert result["ok"] is False and "401 no credit" in result["error"]


def test_the_preflight_sends_no_image():
    """A probe should cost a fraction of a cent, not a ticket's worth of tokens."""
    client = _client()
    with _Router(client):
        vision.check_connection()
    content = client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
    assert all(b["type"] != "image_url" for b in content)
