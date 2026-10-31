"""The vision read — handwriting, header fields, prices, totals, quantity.

One call per ticket. The system prompt instructs JSON-only output with per-field
{value, confidence} and null for anything unreadable. We parse defensively:
strip fences, json.loads, never trust prose. Model confidence is an INPUT to
scoring, not the final cell colour.

TWO PROVIDERS, ONE PIPELINE. `VISION_PROVIDER` selects Anthropic (the default,
so a deployment that sets nothing behaves exactly as it did) or OpenRouter, for
open-weight models at roughly a tenth of the cost. Only the transport differs:
building the request and reading the answer are per-provider, and everything
that took two outages to get right — the error marker, the truncation check, the
retry classification, the trace — is shared. A second copy of that logic is a
second place for a failure to go quiet.

If no reader is configured (OFFLINE_MODE / no key) this returns an empty,
well-formed result so the deterministic path still produces a sheet.

Failures are never silent. Every empty result says why it is empty, because a
whole batch once came back with nothing but barcode data and reported success:
an unread ticket and a blank ticket produced byte-identical output, so 54% of
the deliverable went red with no indication that the extraction had never run.
"""
from __future__ import annotations

import base64
import json
import logging

from app.config import settings
from app.pipeline import modelnames, transient

log = logging.getLogger("pipeline.vision")

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# OUR TICKETS ARE NOT TRAINING DATA, and that is sent on every request rather
# than assumed. OpenRouter is a ROUTER — it hands the request to one of several
# upstream inference providers, and their policies differ. "deny" restricts
# routing to providers that do not store prompts for training. Left unset it
# defaults to "allow", so the safe answer is only true if it is written down.
#
# WHAT THIS DOES NOT COVER, and it is half the answer: OpenRouter's OWN logging
# is an ACCOUNT setting (Settings -> Privacy), not a request field. No code here
# can set it. That matters more for this app than for the dashboard this pattern
# came from, because these requests carry the patient sticker — see the PHI note
# in CLAUDE.md.
#
# A CONSTANT, NOT A SETTING. An env var that loosens this is a switch somebody
# flips to make a failing model work and never flips back.
OPENROUTER_PRIVACY = {"data_collection": "deny"}


# Dollars per MILLION tokens, (input, output). Used only to turn the token
# counts we already receive into a number somebody can act on.
#
# A LOOKUP, NOT A CONTRACT. Published prices move and models get renamed, so an
# unknown model reports its TOKENS and no dollar figure rather than a confident
# wrong one — see estimate_cost. Correct these when a provider changes them;
# nothing breaks if they are briefly stale except the estimate's accuracy.
# Checked 2026-09-30.
MODEL_PRICES = {
    "qwen/qwen3-vl-235b-a22b-instruct": (0.20, 0.88),
    "qwen/qwen3-vl-32b-instruct": (0.104, 0.416),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    # $0.10/$0.50 up to a 100K-token prompt, $0.50/$2.50 above it. One ticket
    # photo plus this prompt is nowhere near 100K, so the lower tier is the
    # one that applies here.
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-haiku-4-5": (1.00, 5.00),
}


# Prompt-cache multipliers on the input price. Anthropic bills cached prompt
# tokens apart from input_tokens (which EXCLUDES them): a 5-minute cache write
# costs 1.25x, a cache read 0.1x — 0.05x on Opus 5.5. Leaving them out made
# every escalated read look cheaper than it was, because the system prompt is
# cached on every Anthropic call.
CACHE_WRITE_MULT = 1.25
CACHE_READ_MULT = {"claude-opus-5-5": 0.05}
CACHE_READ_MULT_DEFAULT = 0.10


def estimate_cost(model: str | None, tokens_in, tokens_out,
                  cache_read=None, cache_write=None) -> float | None:
    """Dollars for one call, or None when we cannot say honestly.

    None rather than 0.0: a zero would quietly understate a bill, and the whole
    point of this is to stop a spend cap arriving as a surprise.
    """
    name = (model or "").strip()
    price = MODEL_PRICES.get(name)
    if not price or tokens_in is None or tokens_out is None:
        return None
    read_mult = CACHE_READ_MULT.get(name, CACHE_READ_MULT_DEFAULT)
    input_usd = (tokens_in
                 + (cache_write or 0) * CACHE_WRITE_MULT
                 + (cache_read or 0) * read_mult) * price[0]
    return round((input_usd + tokens_out * price[1]) / 1_000_000, 6)


def _provider() -> str:
    name = (settings.vision_provider or "").strip().lower()
    return name if name in ("anthropic", "openrouter") else "anthropic"


def _openrouter_models() -> list[str]:
    """The model list OpenRouter walks: the chosen model first, its backup second.

    The backup is dropped when it is the same string as the primary (naming one
    model twice asks the router to retry the thing that just failed) and when it
    is turned off with "none".
    """
    model = (settings.openrouter_model or "").strip()
    backup = (settings.openrouter_fallback_model or "").strip()
    if not backup or backup.lower() == "none" or backup == model:
        return [model]
    return [model, backup]


def vision_configuration() -> tuple[dict | None, str | None]:
    """``(config, None)`` when a reader is configured, ``(None, reason)`` when not.

    A reason rather than an exception, shaped like email.email_configuration():
    "not configured yet" is a normal state this app ships in, and the screen has
    to be able to name the missing variable rather than render a traceback. The
    variable NAMES travel with the configuration — telling an administrator to
    check ANTHROPIC_MODEL on a box running OpenRouter sends them to a setting
    that does not exist.
    """
    if settings.offline_mode:
        return None, "OFFLINE_MODE is on, so tickets are read without an AI model."

    provider = _provider()
    if provider == "openrouter":
        if not (settings.openrouter_api_key or "").strip():
            return None, ("VISION_PROVIDER is openrouter but OPENROUTER_API_KEY "
                          "is not set on the server.")
        model = (settings.openrouter_model or "").strip()
        if not model:
            return None, "OPENROUTER_MODEL is set to an empty value."
        return {
            "provider": "openrouter",
            "model": model,
            "models": _openrouter_models(),
            "key_variable": "OPENROUTER_API_KEY",
            "model_variable": "OPENROUTER_MODEL",
        }, None

    if not (settings.anthropic_api_key or "").strip():
        return None, "ANTHROPIC_API_KEY is not set on the server."
    return {
        "provider": "anthropic",
        # ANTHROPIC_MODEL may name a family; what travels from here on is
        # always the concrete id, so everything downstream — the request, the
        # trace, the price lookup — sees one unambiguous name.
        "model": modelnames.resolve(settings.anthropic_model),
        "spec": (settings.anthropic_model or "").strip(),
        "key_variable": "ANTHROPIC_API_KEY",
        "model_variable": "ANTHROPIC_MODEL",
    }, None

SYSTEM_PROMPT = """\
You extract fields from an orthopedic implant usage ticket (Maxx Orthopedics or
Maxx Health).

The ticket carries a patient sticker. Read NOTHING from it except the patient's
two initials, and return nothing else about the patient in any field, under any
circumstances — not the name, date of birth, medical record number, account
number, sex, admission date or address.

Return ONLY a JSON object, no prose and no markdown fences. For every field
return {"value": <value or null>, "confidence": "high"|"medium"|"low"}.
Use null when you cannot read a field — do NOT guess. Confidence reflects how
clearly legible the source is.

Shape:
{
  "header": {
    "entity": {...}, "rep": {...}, "rep_code": {...}, "surgeon": {...},
    "hospital": {...}, "surgery_date": {...}, "po_number": {...},
    "patient_initials": {...}
  },
  "lines": [ {"index": <int>, "ref": {...}, "lot": {...}, "qty": {...},
             "unit_price": {...}, "wasted": {...}, "description": {...}} ],
  "freight": {...},
  "grand_total": {...}
}

Header fields to read off the (mostly handwritten) header:
  - "surgeon": the surgeon's name as written, usually just the last name (e.g.
    "Montijo"). Read it exactly; do not expand or correct it.
  - "rep_code": the Rep / Distributor code (e.g. "MC-001", "GR-MO-001"). Normalize
    surrounding spaces but keep the characters exactly.
  - "surgery_date": the date of surgery.
  - "hospital": the hospital/facility as written (a cross-check only).

For each device label, read the PRINTED catalogue/reference number and lot:
  - "ref": the REF / catalogue number printed on the label (e.g. "RAUUX412-RK",
    "MO-MSFC-52/MM"). It is printed text, usually labelled "REF" — read it exactly,
    character for character; do not guess or expand it.
  - "lot": the lot/batch number printed on the label (usually labelled "LOT").
  - "unit_price": the HANDWRITTEN price written near that label, in or next to the
    "Price" box. See the price rules below.
  - "wasted": true if a handwritten "W", "wasted", or "I/O" appears near the
    component (the item is still used — just mark it); otherwise false.
  - "qty": the handwritten quantity for this item IF a count is written (e.g. "4",
    "x4", "Qty 4" — common for unlabeled items like "4 pins"). Return an integer.
    Return null when no count is written (the line is a single unit).
Read these from the printed label text even when a barcode is present. Do NOT
provide a description for a label line — it is looked up from the reference
tables.

HANDWRITTEN LINES. Not every line has a label. The form has its own blank
"Ref #", "Lot #", "Description" and "Price" fields, and non-implant items —
pins, screws, instruments, disposables — are written into them by hand. These
are real billable lines and MUST be returned, in the same "lines" array, after
the labelled ones. They are easy to miss because nothing is stuck to the page;
look for handwriting on the form's own ruled blanks.
  - "ref": the handwritten Ref # exactly as written, e.g. "MF-DHXX00D",
    "MF-DAXX00F". Keep the letters as written — an "XX" in the middle is part of
    the real catalogue number, not a placeholder.
  - "description": for THESE lines only, return the handwritten description
    (e.g. "short headed pins", "threaded pins"). Omit any quantity or unit price
    from it. These parts are often absent from the reference tables, so the
    written words are the only description there will ever be.
  - "qty" and "unit_price": the count and the per-item price. They are usually
    written together in the description area as "(x2) 25ea" — meaning 2 items at
    25 each — while the figure in the "Price" box is the LINE TOTAL for all of
    them. So "(x2) 25ea" with "Price: $50.00" is qty 2 and unit_price 25, NOT
    unit_price 50. If only a line total and a count are given, divide. If no
    count is written, qty is null and unit_price is the price as written.
  - "lot": whatever is written on the Lot # blank, usually null.

Price rules (these are handwritten and the most important figures on the ticket):
  - Return the numeric amount only: no "$", no commas, no words. "$1,900.00" -> 1900,
    "1,900" -> 1900, "68" -> 68. Keep cents if written ("68.50" -> 68.5).
  - A price that is crossed out / struck through, or written as "0", "Ø", "∅", "-",
    or "N/C" means NO CHARGE: return 0 for that line's unit_price (do not omit the line).
  - The "$" currency sign is NOT a digit: "$650" is 650, never 8650. If the first
    mark could be either a "$" or an "8", read it as "$" and lower the confidence.
  - Read each price for the label it sits beside; keep "lines" ordered top-to-bottom
    and align each price to its own label. Skip empty slots that read
    "Place Implant Label".
  - "grand_total": the handwritten total, usually bottom-right next to "Grand Total".
  - "freight": the handwritten "Freight/Delivery Fee" if present, else null.
  - If unsure of a digit, set a lower confidence rather than guessing — the line
    prices are reconciled against the grand total downstream.

Secondary / partner billing labels: some tickets include an additional sticker
from a partner company (e.g. a UNIKO instrument kit label). These are text-only
— they carry a printed part number (REF) but no GS1 barcode. Include them as
lines in the same "lines" array. IMPORTANT: always append these AFTER all of the
main barcoded Maxx implant lines, even if the sticker appears physically beside an
earlier label. For these lines:
  - "ref": the printed part/catalogue number (e.g. "UKI0201-L") — read exactly.
  - "lot": null (these labels usually carry no lot number).
  - "unit_price": the handwritten price if one is written next to it, else null.
  - "wasted": false unless a "W" or "I/O" is marked.
  - "qty": null unless a count is written.

"patient_initials" is exactly two uppercase letters: the first letter of the
given name and the first letter of the family name, from the patient sticker.
Ignore middle names and middle initials. Return null if you cannot read both
names clearly — do not guess. Two letters and nothing more.

Dates as ISO YYYY-MM-DD. "lines" is ordered top-to-bottom: labelled implant
lines first, then secondary partner labels, then handwritten form lines.

The grand total is the sum of every line INCLUDING the handwritten ones, so if
your line prices do not reconcile with it, the usual cause is a handwritten line
that was not read. "description" is null on every line except the handwritten
ones.
"""

def _empty(error: str | None = None) -> dict:
    """A well-formed result with nothing in it.

    ``error`` is the difference between "this ticket was blank" and "we never
    got an answer". Before it existed, a missing API key, an HTTP 500 and an
    unparseable response all produced the identical dict, so a total extraction
    outage was indistinguishable from nine genuinely empty tickets — which is
    exactly how one went unnoticed for a whole batch. Callers that see an error
    set MUST surface it; see assemble._vision_failure_flag.
    """
    return {
        "header": {
            "entity": {"value": None, "confidence": "low"},
            "rep": {"value": None, "confidence": "low"},
            "rep_code": {"value": None, "confidence": "low"},
            "surgeon": {"value": None, "confidence": "low"},
            "hospital": {"value": None, "confidence": "low"},
            "surgery_date": {"value": None, "confidence": "low"},
            "po_number": {"value": None, "confidence": "low"},
            "patient_initials": {"value": None, "confidence": "low"},
        },
        "lines": [],
        "freight": {"value": None, "confidence": "low"},
        "grand_total": {"value": None, "confidence": "low"},
        "error": error,
    }


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        # drop the first fence line and any closing fence
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _parse(text: str, stop_reason: str | None = None) -> dict:
    """Parse the model's JSON, or say why we couldn't.

    A truncated response is the failure mode worth naming: thinking and output
    share one max_tokens allowance, so a long deliberation can cut the JSON off
    mid-object. That used to come back as a silent empty result — a billed call
    that looked exactly like a blank ticket.
    """
    if stop_reason == "max_tokens":
        log.error("vision response truncated at max_tokens (%d chars of text)", len(text))
        return _empty(error="response truncated at max_tokens")
    try:
        return json.loads(_strip_fences(text))
    except Exception as e:
        log.error("vision response was not JSON (%s); first 200 chars: %r", e, text[:200])
        return _empty(error=f"unparseable response: {e}")


def _escalate_specs() -> list[str]:
    """The rungs as configured, before any of them is resolved or filtered.

    VISION_ESCALATE_MODEL (singular) still wins when it is set, so a .env that
    pins one reader keeps behaving exactly as it did.
    """
    one = (settings.vision_escalate_model or "").strip()
    raw = one or (settings.vision_escalate_models or "")
    return [r.strip() for r in raw.split(",") if r.strip()]


def escalation_ladder() -> tuple[list[dict], str | None]:
    """The stronger readers in order, or why there are none.

    A ladder rather than one reader, because the two questions have different
    prices. Sonnet costs half what Opus does; most of what the open-weight
    model misses, Sonnet can read. Opus is then paid only for the cells that
    survived both — which is the smallest set anyone has reason to pay Opus for.

    Each rung's model may be a family name; `modelnames.resolve` turns that into
    whatever is current today.
    """
    specs = _escalate_specs()
    if not specs or [s.lower() for s in specs] == ["none"]:
        return [], "escalation is turned off (VISION_ESCALATE_MODELS)"
    if settings.offline_mode:
        return [], "OFFLINE_MODE is on"
    if not (settings.anthropic_api_key or "").strip():
        return [], "ANTHROPIC_API_KEY is not set, so there is nothing to escalate to"

    # Re-reading with the model that just read the ticket is paying twice for
    # one answer. Easy to arrange by accident: VISION_PROVIDER=anthropic with
    # ANTHROPIC_MODEL already equal to a rung. Such a rung is dropped and the
    # REST of the ladder still runs — an escalation to Opus should not be lost
    # because the Sonnet rung happened to duplicate the primary.
    primary, _ = vision_configuration()
    already = set()
    if primary and primary["provider"] == "anthropic":
        already.add((primary["model"] or "").strip())

    ladder, dropped = [], []
    for spec in specs:
        if spec.lower() == "none":
            continue
        model = modelnames.resolve(spec)
        if not model or model in already:
            dropped.append(model or spec)
            continue
        already.add(model)
        ladder.append({"provider": "anthropic", "model": model, "spec": spec})
    if not ladder:
        return [], ("every configured second reader is the same model that "
                    f"already read the ticket ({', '.join(dropped)}), so a "
                    "second read would ask the same model the same question")
    return ladder, None


def escalation_configuration() -> tuple[dict | None, str | None]:
    """The first rung of the ladder, or why there isn't one."""
    ladder, why = escalation_ladder()
    return (ladder[0], None) if ladder else (None, why)


def escalate_fields() -> set[str]:
    """The cells worth paying a second reader for.

    A blank Lot or Expiry is recoverable — from the barcode, from the Expiry
    Log. These are not recoverable from anywhere, and they are what the invoice
    is built from.
    """
    raw = settings.vision_escalate_fields or ""
    return {f.strip() for f in raw.split(",") if f.strip()}


def _first_non_null(*fields):
    """The first field that actually carries a value.

    The escalated read wins where it read something, and the primary fills what
    the escalation missed — so the merge is never worse than either read alone.
    """
    for f in fields:
        if isinstance(f, dict) and f.get("value") not in (None, ""):
            return f
    for f in fields:
        if f is not None:
            return f
    return {"value": None, "confidence": "low"}


def merge_reads(primary: dict, escalated: dict) -> dict:
    """Combine two reads of one ticket, escalation first.

    Field by field rather than wholesale: the stronger reader is better on
    average, not on every single cell, and a field the primary got right should
    not regress because the second reader happened to miss it.
    """
    out = dict(escalated)
    out["header"] = {}
    for key in set(primary.get("header") or {}) | set(escalated.get("header") or {}):
        out["header"][key] = _first_non_null(
            (escalated.get("header") or {}).get(key),
            (primary.get("header") or {}).get(key))
    for key in ("freight", "grand_total"):
        out[key] = _first_non_null(escalated.get(key), primary.get(key))

    plines = primary.get("lines") or []
    elines = escalated.get("lines") or []
    merged = []
    for i in range(max(len(plines), len(elines))):
        p = plines[i] if i < len(plines) else {}
        e = elines[i] if i < len(elines) else {}
        row = {**p, **{k: v for k, v in e.items() if v is not None}}
        for key in set(p) | set(e):
            if isinstance(p.get(key), dict) or isinstance(e.get(key), dict):
                row[key] = _first_non_null(e.get(key), p.get(key))
        merged.append(row)
    out["lines"] = merged
    out["error"] = None
    # Both calls were paid for; the merged ticket has to account for both or
    # escalation would look free in the numbers.
    out["usage"] = (primary.get("usage") or []) + (escalated.get("usage") or [])
    return out


def _request_params(model: str | None = None) -> dict:
    """The model/thinking/effort parameters, in ONE place.

    The preflight below has to send exactly what the real extraction sends, or
    it cannot catch a parameter the API rejects — which is the only reason it
    exists. Two copies of this dict would drift, and the drift would be
    invisible until a batch failed.
    """
    return {
        "model": model or modelnames.resolve(settings.anthropic_model),
        # Adaptive is the ONLY on-mode for this model family. A fixed
        # `{"type": "enabled", "budget_tokens": N}` budget is rejected with a
        # 400 — that shipped once and every call failed for a whole release.
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "high"},
    }


def _b64(img: bytes) -> str:
    return base64.standard_b64encode(img).decode("ascii")


USER_INSTRUCTION = "Extract the fields as instructed. JSON only."

# OpenAI-compatible finish reasons, mapped onto the vocabulary _parse already
# speaks. One vocabulary, so the truncation check and the refusal check work
# identically whoever answered.
_FINISH_REASONS = {
    "length": "max_tokens",
    "content_filter": "refusal",
    "stop": "end_turn",
}


def _call_anthropic(img: bytes, media_type: str, probe: bool = False,
                    model: str | None = None):
    """(text, stop_reason, usage) from Anthropic. Transport only."""
    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    if probe:
        client.messages.create(
            max_tokens=1024, timeout=30.0,
            messages=[{"role": "user", "content": "Reply with the word: ok"}],
            **_request_params(model),
        )
        return "", None, {}

    resp = client.messages.create(
        **_request_params(model),
        # max_tokens covers thinking AND output together, and an 8000 cap is
        # what is believed to have emptied a whole batch: a longer system
        # prompt bought longer deliberation, the JSON was cut off mid-object,
        # and the parser turned that into an empty result. 16000 is the
        # documented default for a non-streaming request.
        max_tokens=16000,
        # A hung call otherwise holds one of the three batch workers for the
        # SDK default (10 minutes).
        timeout=180.0,
        # SYSTEM_PROMPT is static and identical on every call (one per ticket,
        # ~100/day) — cache it so repeat extractions reuse it at ~10% of the
        # input-token cost instead of reprocessing it each time.
        system=[{"type": "text", "text": SYSTEM_PROMPT,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64",
                                             "media_type": media_type,
                                             "data": _b64(img)}},
                {"type": "text", "text": USER_INSTRUCTION},
            ],
        }],
    )
    text = "".join(b.text for b in resp.content
                   if getattr(b, "type", None) == "text")
    stop_reason = getattr(resp, "stop_reason", None)
    if stop_reason == "refusal":
        details = getattr(resp, "stop_details", None)
        stop_reason = f"refusal:{getattr(details, 'category', None) or 'unspecified'}"
    u = getattr(resp, "usage", None)
    return text, stop_reason, {
        "tokens_in": getattr(u, "input_tokens", None),
        "tokens_out": getattr(u, "output_tokens", None),
        "cache_read": getattr(u, "cache_read_input_tokens", None),
        "cache_write": getattr(u, "cache_creation_input_tokens", None),
        # Which model actually answered. It matters more now that a setting can
        # name a family: the bill is for what ran, not for what we asked for.
        "served_by": getattr(resp, "model", None),
    }


def _call_openrouter(img: bytes, media_type: str, probe: bool = False,
                     model: str | None = None):
    """(text, stop_reason, usage) from OpenRouter. Transport only.

    OpenRouter implements the OpenAI chat-completions surface, so the official
    OpenAI SDK is the client — pointed at a different base URL. There is no
    prompt caching to ask for here and no thinking budget; the whole request is
    the system prompt, the image and one instruction.
    """
    import openai

    client = openai.OpenAI(api_key=settings.openrouter_api_key,
                           base_url=OPENROUTER_BASE_URL, timeout=180.0)
    content = [{"type": "text", "text": USER_INSTRUCTION}]
    if not probe:
        content.insert(0, {
            "type": "image_url",
            "image_url": {"url": f"data:{media_type};base64,{_b64(img)}"},
        })
    resp = client.chat.completions.create(
        model=model or modelnames.resolve(settings.openrouter_model),
        max_tokens=1024 if probe else 16000,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        extra_headers={"X-Title": "Maxx Usage Tickets"},
        extra_body={
            # See OPENROUTER_PRIVACY: routing is restricted to providers that
            # do not keep prompts for training, on every single request.
            "provider": OPENROUTER_PRIVACY,
            # The router walks this list when the first model cannot serve the
            # request. One request, not a second retry loop in our code.
            "models": _openrouter_models(),
        },
    )
    if probe:
        return "", None, {}
    choice = resp.choices[0]
    finish = getattr(choice, "finish_reason", None)
    u = getattr(resp, "usage", None)
    return choice.message.content or "", _FINISH_REASONS.get(finish, finish), {
        "tokens_in": getattr(u, "prompt_tokens", None),
        "tokens_out": getattr(u, "completion_tokens", None),
        "cache_read": None,
        "cache_write": None,
        # Which model actually answered — the router may have walked to the
        # backup, and a quality question is unanswerable without knowing that.
        "served_by": getattr(resp, "model", None),
    }


_TRANSPORTS = {"anthropic": _call_anthropic, "openrouter": _call_openrouter}


def check_connection() -> dict:
    """Can we actually talk to the model? Returns {ok, model, error}.

    One small call through the SAME transport a real extraction uses, so a
    parameter the provider rejects is caught here rather than by a batch.

    It exists because this call has failed twice at the boundary between our
    process and the API — once on a token cap, once on a parameter the model
    rejects — and neither was visible from inside the process. A mocked test
    suite cannot validate an API contract; only a real call can. The error
    string is passed through verbatim, because the exact text is the diagnosis.
    """
    cfg, reason = vision_configuration()
    if cfg is None:
        if settings.offline_mode:
            return {"ok": True, "model": None, "error": None, "detail": reason}
        return {"ok": False, "model": None, "error": reason}
    try:
        _TRANSPORTS[cfg["provider"]](b"", "image/jpeg", probe=True)
        out = {"ok": True, "model": cfg["model"], "error": None,
               "provider": cfg["provider"]}
    except Exception as e:
        log.error("vision connection check failed: %s", e)
        return {"ok": False, "model": cfg["model"],
                "provider": cfg["provider"], "error": f"{type(e).__name__}: {e}"}

    # The second readers get probed too, and this is why: a rung may name a
    # FAMILY, so which model answers it is decided at run time and can change
    # without anyone editing a setting. If a newly released model rejects a
    # parameter this app sends, the only alternative to finding out here is
    # finding out when a ticket escalates in the middle of a batch.
    #
    # Deliberately NOT part of `ok`: escalation is an improvement on the
    # primary read, so a broken rung must not stop a batch that can still be
    # read. run_batch gates on `ok` alone.
    out["escalation"] = _probe_ladder()
    return out


def _probe_ladder() -> list[dict]:
    ladder, why = escalation_ladder()
    if not ladder:
        return [{"model": None, "ok": False, "error": why}] if why else []
    checked = []
    for rung in ladder:
        try:
            _TRANSPORTS[rung["provider"]](b"", "image/jpeg", probe=True,
                                          model=rung["model"])
            checked.append({"model": rung["model"], "spec": rung["spec"],
                            "ok": True, "error": None})
        except Exception as e:
            log.error("second reader %s is unreachable: %s", rung["model"], e)
            checked.append({"model": rung["model"], "spec": rung["spec"],
                            "ok": False, "error": f"{type(e).__name__}: {e}"})
    return checked


def extract_handwritten(img_bytes: bytes, media_type: str = "image/jpeg",
                        escalate: bool | dict = False) -> dict:
    """One call to the configured reader. Returns the per-field result.

    ``escalate`` sends the ticket to a stronger reader instead of the primary:
    ``True`` for the first rung of the ladder, or a rung dict from
    ``escalation_ladder()`` to name which one. Everything else about the call —
    the prompt, the parsing, the error marker, the trace — is identical,
    because a second reader that reported its failures differently would be a
    second thing to keep right.

    Always a well-formed result, so downstream code is uniform whether or not a
    reader is configured — but a result that came back empty because something
    went wrong carries ``error``, and callers must surface it. A transient
    failure is raised instead, so the ticket is retried rather than recorded as
    empty.
    """
    from app.pipeline import tracer

    if isinstance(escalate, dict):
        cfg, reason = escalate, None
    elif escalate:
        cfg, reason = escalation_configuration()
    else:
        cfg, reason = vision_configuration()
    if cfg is None:
        # OFFLINE_MODE is a deliberate choice — the deterministic path is the
        # whole point of it, so it is not an error. A missing key in a live
        # deployment is a misconfiguration, and saying so is how the next
        # silent outage gets noticed on the first ticket instead of the ninth.
        tracer.record("vision_ai", "Vision AI extraction", "skip",
                      f"Skipped — {reason}", {})
        if settings.offline_mode:
            return _empty()
        log.error("vision unavailable: %s", reason)
        return _empty(error=reason)
    if not img_bytes:
        tracer.record("vision_ai", "Vision AI extraction", "skip",
                      "Skipped — no image bytes for this ticket", {})
        log.warning("vision skipped: no image bytes")
        return _empty(error="no image bytes for this ticket")

    model = cfg["model"]
    try:
        text, stop_reason, usage = _TRANSPORTS[cfg["provider"]](
            img_bytes, media_type, model=model)

        # A declined request comes back as a normal 200 with no text, so it
        # would otherwise fall through to the parser and be reported as
        # "unparseable response" — true, but it would send the next person
        # hunting in the wrong place. Say what actually happened.
        if str(stop_reason or "").startswith("refusal"):
            category = str(stop_reason).partition(":")[2] or "unspecified"
            log.error("vision request was declined (category=%s)", category)
            result = _empty(error=f"request declined by the model ({category})")
        else:
            result = _parse(text, stop_reason)

        err = result.get("error")
        line_count = len(result.get("lines") or [])
        served = usage.get("served_by") or model
        tokens_in, tokens_out = usage.get("tokens_in"), usage.get("tokens_out")
        token_str = (f" | {tokens_in}↑ {tokens_out}↓ tokens"
                     if tokens_in is not None else "")
        if usage.get("cache_read"):
            token_str += f" ({usage['cache_read']} cached)"
        tracer.record(
            "vision_escalation" if escalate else "vision_ai",
            f"{'Second read' if escalate else 'Vision AI extraction'} ({served})",
            "fail" if err else ("ok" if line_count > 0 else "warn"),
            (f"FAILED — {err}" if err
             else f"{served} — {line_count} line(s) found{token_str}"),
            {
                "provider": cfg["provider"],
                "model": model,
                "served_by": served,
                "stop_reason": stop_reason,
                "error": err,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cache_read_input_tokens": usage.get("cache_read"),
                "cache_creation_input_tokens": usage.get("cache_write"),
                "header": result.get("header"),
                "lines": result.get("lines"),
                "freight": result.get("freight"),
                "grand_total": result.get("grand_total"),
            },
        )
        # Carried on the result so the ticket can record what it cost. The
        # counts were already being computed for the trace and then dropped,
        # which is why "why is this costing what it is" had no answer inside
        # the app — only in a provider's console, after the fact.
        result["usage"] = [{
            "provider": cfg["provider"],
            "model": served,
            "escalated": bool(escalate),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cache_read": usage.get("cache_read"),
            "cache_write": usage.get("cache_write"),
            "usd": estimate_cost(served, tokens_in, tokens_out,
                                 usage.get("cache_read"),
                                 usage.get("cache_write")),
        }]
        if err:
            log.error("vision extraction produced no usable result: %s", err)
        return result
    except Exception as e:
        # A transient failure is re-raised so run._safe_process can retry the
        # ticket. It could never do that before: this handler swallowed the
        # RateLimitError / APITimeoutError / 529 that _is_transient was written
        # to catch, so the retry loop was unreachable from the vision path.
        if transient.is_transient(e):
            log.warning("transient vision failure, will retry: %s", e)
            raise
        # Anything else: the batch still finishes, but the ticket is marked so
        # nobody mistakes an outage for a blank ticket.
        log.exception("vision extraction failed")
        return _empty(error=f"{type(e).__name__}: {e}")
