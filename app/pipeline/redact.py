"""The PHI gate. Runs before anything else touches the image.

Policy (LABEL_EXTRACTION_BUILD_SPEC §9): we do not process patient data at all.
The patient sticker is masked at ingest, before the image is read, sent to the
vision API, or stored. Fail safe: if we can't confidently locate the patient
region on a recognized template, we return located=False and the caller routes
the ticket to the manual queue and sends the image nowhere.
"""
from __future__ import annotations

from app.pipeline import preprocess
from app.pipeline.template import geometry_for, is_known


def redact_patient_region(img, template: str):
    """Mask the patient sticker for the detected template.

    Returns (redacted_image, located). If located is False the caller MUST route
    the ticket to manual_queue and NOT send the image anywhere.
    """
    if img is None or not preprocess.available():
        # No image decoded (cv2 missing or undecodable) -> cannot prove the
        # region was masked. Fail safe.
        return img, False

    if not is_known(template):
        return img, False

    geom = geometry_for(template)
    if geom is None:
        return img, False

    import cv2  # available because preprocess.available() is True
    import numpy as np

    h, w = img.shape[:2]
    if h == 0 or w == 0:
        return img, False

    x, y, rw, rh = geom.patient_region.to_pixels(w, h)
    # Clamp to image bounds.
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(w, x + rw), min(h, y + rh)
    if x1 <= x0 or y1 <= y0:
        return img, False

    redacted = img.copy()
    # Solid black fill — irreversible, no patient pixels survive downstream.
    cv2.rectangle(redacted, (x0, y0), (x1, y1), (0, 0, 0), thickness=-1)
    # Visible label so a human reviewer understands the black box is intentional.
    cv2.putText(
        redacted,
        "PATIENT INFO REMOVED",
        (x0 + 8, min(y1 - 8, y0 + 24)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.5,
        (255, 255, 255),
        1,
        cv2.LINE_AA,
    )
    located = bool(np.any(redacted != img))
    return redacted, located
