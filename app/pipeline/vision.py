"""Claude vision fallback — handwriting, header fields, prices, totals, quantity.

Single Claude call per ticket. The system prompt (verbatim from DEVELOPER_HANDOFF
§7) instructs JSON-only output with per-field {value, confidence} and null for
anything unreadable. We parse defensively: strip fences, json.loads, never trust
prose. Model confidence is an INPUT to scoring, not the final cell colour.

If Anthropic isn't configured (OFFLINE_MODE / no key) this returns an empty,
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
from app.pipeline import transient

log = logging.getLogger("pipeline.vision")

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


def extract_handwritten(redacted_img_bytes: bytes, media_type: str = "image/jpeg") -> dict:
    """Single Claude call. Returns the JSON-parsed per-field result.

    Always a well-formed result, so downstream code is uniform whether or not
    the API is configured — but a result that came back empty because something
    went wrong carries ``error``, and callers must surface it. A transient
    failure is raised instead, so the ticket is retried rather than recorded as
    empty.
    """
    if not settings.has_anthropic:
        from app.pipeline import tracer
        # OFFLINE_MODE is a deliberate choice — the deterministic path is the
        # whole point of it, so it is not an error. A missing key in a live
        # deployment is a misconfiguration, and saying so is how the next
        # silent outage gets noticed on the first ticket instead of the ninth.
        deliberate = settings.offline_mode
        reason = ("offline mode" if deliberate
                  else "no Anthropic API key configured")
        tracer.record("vision_ai", "Vision AI extraction", "skip",
                      f"Skipped — {reason}", {})
        if deliberate:
            return _empty()
        log.error("vision unavailable: %s", reason)
        return _empty(error=reason)
    if not redacted_img_bytes:
        from app.pipeline import tracer
        tracer.record("vision_ai", "Vision AI extraction", "skip",
                      "Skipped — no image bytes for this ticket", {})
        log.warning("vision skipped: no image bytes")
        return _empty(error="no image bytes for this ticket")

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        b64 = base64.standard_b64encode(redacted_img_bytes).decode("ascii")
        resp = client.messages.create(
            model=settings.anthropic_model,
            # max_tokens covers thinking AND output together. Adaptive thinking
            # with an 8000 cap is what is believed to have emptied a whole batch:
            # a longer system prompt bought longer deliberation, the JSON was cut
            # off mid-object, and the parser turned that into an empty result. A
            # fixed budget cannot expand to crowd the answer out, and 16000 - 4000
            # leaves far more room for the JSON than any ticket needs.
            max_tokens=16000,
            thinking={"type": "enabled", "budget_tokens": 4000},
            # A hung call otherwise holds one of the three batch workers for the
            # SDK default (10 minutes).
            timeout=180.0,
            # Sonnet 5 defaults to "high" when effort is unset. "medium" is the
            # cost/quality knob for the per-ticket read; watch the amber/red rate
            # in History after changing it — that's the regression signal.
            output_config={"effort": "medium"},
            # SYSTEM_PROMPT is static and identical on every call (one per ticket,
            # ~100/day) — cache it so repeat extractions reuse it at ~10% of the
            # input-token cost instead of reprocessing it each time.
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": "Extract the fields as instructed. JSON only.",
                        },
                    ],
                }
            ],
        )
        text = "".join(
            block.text for block in resp.content if getattr(block, "type", None) == "text"
        )
        stop_reason = getattr(resp, "stop_reason", None)
        result = _parse(text, stop_reason)
        from app.pipeline import tracer
        line_count = len(result.get("lines") or [])
        err = result.get("error")
        usage = getattr(resp, "usage", None)
        tokens_in = getattr(usage, "input_tokens", None)
        tokens_out = getattr(usage, "output_tokens", None)
        cache_read = getattr(usage, "cache_read_input_tokens", None)
        cache_write = getattr(usage, "cache_creation_input_tokens", None)
        token_str = f" | {tokens_in}↑ {tokens_out}↓ tokens" if tokens_in is not None else ""
        if cache_read:
            token_str += f" ({cache_read} cached)"
        tracer.record(
            "vision_ai",
            f"Vision AI extraction ({settings.anthropic_model})",
            "fail" if err else ("ok" if line_count > 0 else "warn"),
            (f"FAILED — {err}" if err
             else f"{settings.anthropic_model} — {line_count} line(s) found{token_str}"),
            {
                "model": settings.anthropic_model,
                "stop_reason": stop_reason,
                "error": err,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_write,
                "header": result.get("header"),
                "lines": result.get("lines"),
                "freight": result.get("freight"),
                "grand_total": result.get("grand_total"),
            },
        )
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
