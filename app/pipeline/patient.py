"""Patient initials — the ONLY place this app reads patient data.

Everything else in the pipeline still holds to LABEL_EXTRACTION_BUILD_SPEC §9
("we do not process patient data at all"). This module is the single, explicit,
flag-gated exception, and it is deliberately as narrow as the requirement allows:

  * OFF unless ``EXTRACT_PATIENT_INITIALS=true``. Default posture is unchanged.
  * Runs only AFTER the redaction gate has confirmed the patient region was
    located. A ticket that fails the gate still sends its image nowhere.
  * Sends ONLY the cropped patient sticker — never the full ticket image.
  * Asks for, and keeps, ONLY two letters. The name, DOB, MRN and every other
    identifier on that sticker are never returned, logged, or persisted.
  * The crop lives in memory for the duration of one API call. The image that
    gets STORED is still the fully redacted one — redact.py is untouched.

If anything at all goes wrong (flag off, no key, unknown template, bad crop,
API error, unparseable answer) this returns no value and the ticket simply has
a blank Inits cell. It never falls back to sending more than the crop.
"""
from __future__ import annotations

import base64
import json
import re

from app.config import settings
from app.pipeline import preprocess
from app.pipeline.template import geometry_for

_NONE = {"value": None, "confidence": "low"}

SYSTEM_PROMPT = """\
You are shown a small crop of a patient sticker from a surgical usage ticket.

Return ONLY the patient's initials: the first letter of the first (given) name
and the first letter of the last (family) name. Ignore middle names and middle
initials entirely.

Return ONLY this JSON object, no prose and no markdown fences:
{"initials": "XY" or null, "confidence": "high"|"medium"|"low"}

Rules:
- "initials" must be exactly two uppercase letters, or null if you cannot read
  both names clearly. Do not guess.
- Return NOTHING else about the patient. Do not return the full name, date of
  birth, medical record number, account number, address, or any other detail
  from the sticker, in any field, under any circumstances.
- confidence reflects how clearly legible the two names are.
"""


def _crop_patient(img, geom):
    """The patient-sticker rectangle for this template, clamped to the image."""
    h, w = img.shape[:2]
    x, y, rw, rh = geom.patient_region.to_pixels(w, h)
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(w, x + rw), min(h, y + rh)
    if x1 <= x0 or y1 <= y0:
        return None
    return img[y0:y1, x0:x1]


def _clean(value) -> str | None:
    """Accept only two letters. Anything else (a full name, a number, three
    letters) is rejected rather than stored — the schema is the safety net."""
    if value is None:
        return None
    s = re.sub(r"[^A-Za-z]", "", str(value))
    return s.upper() if len(s) == 2 else None


def _parse(text: str) -> dict:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    try:
        data = json.loads(t.strip())
    except (ValueError, TypeError):
        return dict(_NONE)
    initials = _clean(data.get("initials"))
    if not initials:
        return dict(_NONE)
    conf = str(data.get("confidence") or "low").lower()
    if conf not in ("high", "medium", "low"):
        conf = "low"
    return {"value": initials, "confidence": conf}


def extract_initials(img, template: str) -> dict:
    """Read the patient's initials off the *unredacted* in-memory image.

    `img` must be the pre-mask image and the caller must already have confirmed
    the redaction gate located the patient region. Returns
    {"value": "JD"|None, "confidence": "high"|"medium"|"low"}.
    """
    if not settings.extract_patient_initials:
        return dict(_NONE)
    if img is None or not settings.has_anthropic:
        return dict(_NONE)

    geom = geometry_for(template)
    if geom is None:
        return dict(_NONE)

    crop = _crop_patient(img, geom)
    if crop is None or getattr(crop, "size", 0) == 0:
        return dict(_NONE)
    crop_bytes = preprocess.encode_image(crop, ".jpg")
    if not crop_bytes:
        return dict(_NONE)

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        resp = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": base64.standard_b64encode(crop_bytes).decode("ascii"),
                        },
                    },
                    {"type": "text", "text": "Return the initials as instructed. JSON only."},
                ],
            }],
        )
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        )
        result = _parse(text)
    except Exception:
        # Never let this sink an upload: the ticket just gets a blank Inits cell.
        return dict(_NONE)

    from app.pipeline import tracer
    tracer.record(
        "patient_initials",
        "Patient initials (patient-sticker crop only)",
        "ok" if result["value"] else "miss",
        (f"read '{result['value']}' ({result['confidence']})" if result["value"]
         else "could not read both names — left blank"),
        # Only the two letters are recorded. The crop and the model's raw text
        # are deliberately not put in the trace.
        {"initials": result["value"], "confidence": result["confidence"]},
    )
    return result
