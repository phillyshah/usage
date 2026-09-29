"""The PHI gate. Runs before anything else touches the image.

Policy (LABEL_EXTRACTION_BUILD_SPEC §9): we do not process patient data at all.
The patient sticker is masked at ingest, before the image is read, sent to the
vision API, or stored. Fail safe: if we can't confidently locate the patient
region on a recognized template, we return located=False and the caller routes
the ticket to the manual queue and sends the image nowhere.
"""
from __future__ import annotations

import logging

from app.pipeline import preprocess
from app.pipeline.template import is_known, patient_mask

log = logging.getLogger("redact")


# How far the detected edge may travel from the geometry's fixed value.
#
# Asymmetric on purpose. Moving the edge OUTWARD (away from the header, toward
# the sticker) only ever un-masks form furniture — the header box's own right
# hand column — so it is allowed a long reach: real tickets put that gutter
# anywhere from 0.58w to 0.65w depending on the layout, and the fixed 0.55w edge
# was cutting through "Surgery Date: 9/28/26" and the surgeon's name on every
# ticket of the second kind. Moving it INWARD would start eating the sticker, so
# it is barely allowed at all.
_DRIFT_OUT = 0.16
_DRIFT_IN = 0.02


def _rule_edge(cv2, np, img, region, w: int, h: int) -> int | None:
    """x of the form's vertical rule between the header box and the sticker.

    Returns None when no convincing rule is found, in which case the caller
    keeps the geometry's fixed edge — which errs toward masking more.
    """
    right_side = region.x >= 0.5
    anchor = region.x if right_side else region.x + region.w
    if right_side:
        lo = int(max(0.0, anchor - _DRIFT_IN) * w)
        hi = int(min(1.0, anchor + _DRIFT_OUT) * w)
    else:
        lo = int(max(0.0, anchor - _DRIFT_OUT) * w)
        hi = int(min(1.0, anchor + _DRIFT_IN) * w)
    if hi - lo < 4:
        return None
    try:
        band = cv2.cvtColor(img[0:max(1, int(region.h * h)), :], cv2.COLOR_BGR2GRAY)
        th = cv2.adaptiveThreshold(band, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                   cv2.THRESH_BINARY_INV, 31, 10)
        # Keep only structures tall enough to be a printed rule, not text.
        vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(3, int(0.06 * h))))
        vert = cv2.morphologyEx(th, cv2.MORPH_OPEN, vk)
        heights = vert.sum(axis=0) / 255.0
    except Exception:  # pragma: no cover - never let detection break the gate
        return None

    strong = [x for x in range(lo, hi) if heights[x] > 0.05 * h]
    if not strong:
        return None
    # The gutter between the header box and the sticker is the LAST rule before
    # the sticker begins, so take the one furthest from the header. Anything
    # further out than that belongs to the sticker's own border and is excluded
    # by the drift cap above.
    return max(strong) if right_side else min(strong)


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

    region = patient_mask(template)
    if region is None:
        return img, False

    import cv2  # available because preprocess.available() is True
    import numpy as np

    h, w = img.shape[:2]
    if h == 0 or w == 0:
        return img, False

    x, y, rw, rh = region.to_pixels(w, h)
    # Clamp to image bounds.
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(w, x + rw), min(h, y + rh)
    if x1 <= x0 or y1 <= y0:
        return img, False

    # Snap the header-facing edge to the form's own printed rule.
    #
    # The gap between where the header handwriting ends and where the sticker
    # begins is narrow (0.58w and 0.61w on the ticket this was built from) and
    # moves with the framing, so a fixed fraction misses in one direction or the
    # other: too far in and a patient's details survive, too far out and the
    # surgery date loses its year — which is what the old 0.55 edge was quietly
    # doing. The rule between the two boxes is a strong vertical line and is far
    # easier to find reliably than the sticker itself.
    edge = _rule_edge(cv2, np, img, region, w, h)
    if edge is not None:
        if region.x < 0.5:          # Health: sticker on the left, refine x1
            x1 = edge
        else:                       # Orthopedics: sticker on the right, refine x0
            x0 = edge
    if x1 <= x0:
        return img, False

    redacted = img.copy()
    # Solid black fill — irreversible, no patient pixels survive downstream.
    cv2.rectangle(redacted, (x0, y0), (x1, y1), (0, 0, 0), thickness=-1)

    # Does the patient zone actually come back clear?
    #
    # This used to be `np.any(redacted != img)` — "did any pixel change" — which
    # is true the moment the rectangle lands anywhere on the page, including
    # squarely beside the sticker. It reported success on a ticket that kept a
    # patient's date of birth, CSN and sex in the stored image, because those
    # lines sat below the rectangle's bottom edge.
    #
    # Now the masked band is re-read: if any ink survives inside it, the fill
    # did not do its job and the caller routes the ticket to the manual queue.
    # Checked BEFORE the caption is drawn, so the only thing that can register
    # here is ink the fill failed to cover. Drawing first made the check depend
    # on the caption's fixed size relative to the band, which is a different
    # thing entirely and fails on small images.
    band = redacted[y0:y1, x0:x1]
    if band.size == 0:
        return img, False
    grey = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY) if band.ndim == 3 else band
    surviving = int((grey > 40).sum())
    located = surviving == 0
    if not located:
        log.warning("patient band kept %d light pixels after masking — routing "
                    "to manual queue", surviving)
        return img, False

    # Only now the label, so a reviewer understands the black box is deliberate.
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
    return redacted, True
