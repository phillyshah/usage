"""Claude vision fallback — handwriting, header fields, prices, totals, quantity.

Single Claude call per ticket. The system prompt (verbatim from DEVELOPER_HANDOFF
§7) instructs JSON-only output with per-field {value, confidence} and null for
anything unreadable. We parse defensively: strip fences, json.loads, never trust
prose. Model confidence is an INPUT to scoring, not the final cell colour.

If Anthropic isn't configured (OFFLINE_MODE / no key) this returns an empty,
well-formed result so the deterministic path still produces a sheet.
"""
from __future__ import annotations

import base64
import json

from app.config import settings

SYSTEM_PROMPT = """\
You extract fields from a redacted orthopedic implant usage ticket (Maxx
Orthopedics or Maxx Health). The patient area has been masked; ignore any
masked region and never infer patient information.

Return ONLY a JSON object, no prose and no markdown fences. For every field
return {"value": <value or null>, "confidence": "high"|"medium"|"low"}.
Use null when you cannot read a field — do NOT guess. Confidence reflects how
clearly legible the source is.

Shape:
{
  "header": {
    "entity": {...}, "rep": {...}, "rep_code": {...}, "surgeon": {...},
    "hospital": {...}, "surgery_date": {...}, "po_number": {...}
  },
  "lines": [ {"index": <int>, "ref": {...}, "lot": {...}, "qty": {...},
             "unit_price": {...}, "wasted": {...}} ],
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
provide a description — that is looked up separately from the reference tables.

Price rules (these are handwritten and the most important figures on the ticket):
  - Return the numeric amount only: no "$", no commas, no words. "$1,900.00" -> 1900,
    "1,900" -> 1900, "68" -> 68. Keep cents if written ("68.50" -> 68.5).
  - A price that is crossed out / struck through, or written as "0", "Ø", "∅", "-",
    or "N/C" means NO CHARGE: return 0 for that line's unit_price (do not omit the line).
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

Dates as ISO YYYY-MM-DD. "lines" is ordered top-to-bottom (main implant labels
first, secondary partner labels appended at the end).
"""

_EMPTY = {
    "header": {
        "entity": {"value": None, "confidence": "low"},
        "rep": {"value": None, "confidence": "low"},
        "rep_code": {"value": None, "confidence": "low"},
        "surgeon": {"value": None, "confidence": "low"},
        "hospital": {"value": None, "confidence": "low"},
        "surgery_date": {"value": None, "confidence": "low"},
        "po_number": {"value": None, "confidence": "low"},
    },
    "lines": [],
    "freight": {"value": None, "confidence": "low"},
    "grand_total": {"value": None, "confidence": "low"},
}


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        # drop the first fence line and any closing fence
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _parse(text: str) -> dict:
    try:
        return json.loads(_strip_fences(text))
    except Exception:
        return json.loads(json.dumps(_EMPTY))  # deep copy of empty


def extract_handwritten(redacted_img_bytes: bytes, media_type: str = "image/jpeg") -> dict:
    """Single Claude call. Returns the JSON-parsed per-field result.

    Empty well-formed result when vision is unavailable, so downstream code is
    uniform whether or not the API is configured.
    """
    if not settings.has_anthropic or not redacted_img_bytes:
        from app.pipeline import tracer
        tracer.record("vision_ai", "Vision AI extraction", "skip",
                      "Skipped — no Anthropic API key configured or empty image", {})
        return json.loads(json.dumps(_EMPTY))

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        b64 = base64.standard_b64encode(redacted_img_bytes).decode("ascii")
        resp = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=8000,
            thinking={"type": "adaptive"},
            system=SYSTEM_PROMPT,
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
        result = _parse(text)
        from app.pipeline import tracer
        line_count = len(result.get("lines") or [])
        usage = getattr(resp, "usage", None)
        tokens_in = getattr(usage, "input_tokens", None)
        tokens_out = getattr(usage, "output_tokens", None)
        token_str = f" | {tokens_in}↑ {tokens_out}↓ tokens" if tokens_in is not None else ""
        tracer.record(
            "vision_ai",
            f"Vision AI extraction ({settings.anthropic_model})",
            "ok" if line_count > 0 else "warn",
            f"{settings.anthropic_model} — {line_count} line(s) found{token_str}",
            {
                "model": settings.anthropic_model,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "header": result.get("header"),
                "lines": result.get("lines"),
                "freight": result.get("freight"),
                "grand_total": result.get("grand_total"),
            },
        )
        return result
    except Exception:
        # Never let a vision failure sink the batch; emit empty + let cells go red.
        return json.loads(json.dumps(_EMPTY))
