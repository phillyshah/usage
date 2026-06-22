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
  "lines": [ {"index": <int>, "ref": {...}, "lot": {...}, "qty": {...}, "unit_price": {...}} ],
  "freight": {...},
  "grand_total": {...}
}

For each device label, read the PRINTED catalogue/reference number and lot:
  - "ref": the REF / catalogue number printed on the label (e.g. "RAUUX412-RK",
    "MO-MSFC-52/MM"). It is printed text, usually labelled "REF" — read it exactly,
    character for character; do not guess or expand it.
  - "lot": the lot/batch number printed on the label (usually labelled "LOT").
Read these from the printed label text even when a barcode is present. Do NOT
provide a description — that is looked up separately from the reference log.

Dates as ISO YYYY-MM-DD. Money as numbers without symbols. "lines" is ordered
top-to-bottom; skip empty slots that read "Place Implant Label".
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
        return json.loads(json.dumps(_EMPTY))

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        b64 = base64.standard_b64encode(redacted_img_bytes).decode("ascii")
        resp = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=2000,
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
        return _parse(text)
    except Exception:
        # Never let a vision failure sink the batch; emit empty + let cells go red.
        return json.loads(json.dumps(_EMPTY))
