"""Template detection + region geometry.

Two ticket layouts circulate: Maxx Orthopedics and Maxx Health. We need to know
which one we're looking at so redaction can mask the right patient-sticker
location and segmentation can find the label grid.

Detection here is intentionally conservative and deterministic. A production
build would key off printed logo/anchor matching; until those reference anchors
are captured (Phase 1 task R), we expose:
  * a relative-rectangle geometry per template, and
  * a best-effort detector with a clear UNKNOWN result.

Regions are expressed as fractional rectangles (x, y, w, h) in [0,1] so they
scale to any photo resolution.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

MAXX_ORTHO = "Maxx Orthopedics"
MAXX_HEALTH = "Maxx Health"
UNKNOWN = "Unknown"


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    def to_pixels(self, width: int, height: int) -> tuple[int, int, int, int]:
        return (
            int(self.x * width),
            int(self.y * height),
            int(self.w * width),
            int(self.h * height),
        )


@dataclass(frozen=True)
class TemplateGeometry:
    name: str
    # Patient sticker region to redact (PHI). Conservative/oversized on purpose.
    #
    # NOTE: this is the *anchor*, not the mask. redact.py masks the whole band
    # from this rectangle's left edge to the image edge, from the top of the
    # image down to the top of grid_region — see patient_mask(). A fixed
    # rectangle cannot survive a differently-framed photo, and one that came up
    # short left a patient's date of birth visible in a stored image.
    patient_region: Rect
    # Header block (handwritten fields + entity) for the vision call context.
    header_region: Rect
    # Label grid area where device labels live.
    grid_region: Rect


# Anchor geometries. These are the documented defaults; tune against real
# fixtures during Phase 1 task R. Patient regions are deliberately generous so
# we never under-mask PHI.
_GEOMETRY: dict[str, TemplateGeometry] = {
    MAXX_ORTHO: TemplateGeometry(
        name=MAXX_ORTHO,
        patient_region=Rect(0.55, 0.05, 0.42, 0.16),
        header_region=Rect(0.02, 0.04, 0.52, 0.20),
        grid_region=Rect(0.02, 0.26, 0.96, 0.70),
    ),
    MAXX_HEALTH: TemplateGeometry(
        name=MAXX_HEALTH,
        patient_region=Rect(0.04, 0.05, 0.42, 0.16),
        header_region=Rect(0.46, 0.04, 0.52, 0.20),
        grid_region=Rect(0.02, 0.26, 0.96, 0.70),
    ),
}


def geometry_for(template: str) -> TemplateGeometry | None:
    return _GEOMETRY.get(template)


def patient_mask(template: str) -> Rect | None:
    """The region redact.py actually blacks out.

    Everything on the patient's side of the sheet, above the label grid. On both
    layouts that band holds exactly two things: the patient sticker, and Maxx's
    own printed contact boilerplate (phone, fax, "Email PO's to ..."). Nothing
    the extraction reads — rep, rep code, hospital, surgery date and surgeon all
    sit in header_region on the opposite side, and the labels all sit below in
    grid_region.

    So the whole band can go, which is what makes this framing-independent: the
    sticker is covered wherever it lands in that zone, instead of only when the
    photo happens to be cropped the way the fixed rectangle assumed.
    """
    geom = _GEOMETRY.get(template)
    if geom is None:
        return None
    p, grid = geom.patient_region, geom.grid_region
    # Health's sticker is on the left, Orthopedics' on the right: extend away
    # from the header, to whichever image edge the sticker side faces.
    if p.x < 0.5:
        x0, x1 = 0.0, p.x + p.w
    else:
        x0, x1 = p.x, 1.0
    return Rect(x0, 0.0, x1 - x0, grid.y)


def detect_template(img, filename: str | None = None) -> str:
    """Best-effort template detection.

    Order of evidence:
      1. Entity prefix on the filename (the production naming convention).
      2. Entity word anywhere in the filename (descriptively named files).
      3. (future) printed-logo anchor match via OpenCV template matching.
    Returns one of MAXX_ORTHO / MAXX_HEALTH / UNKNOWN. UNKNOWN must NOT be
    treated as redactable — callers route it to the manual queue.

    Getting this wrong is a PHI problem, not just an accuracy one: the two
    layouts are mirror images, so a Health ticket read as Orthopedics masks the
    empty right-hand side and leaves the patient sticker in full view.
    """
    name = os.path.basename(filename or "").lower()

    # Tickets are named by entity prefix + ticket number ("MH17469.jpg",
    # "MO083596.jpg"), and each page of a multi-page PDF keeps it ("MH17469-p2").
    # Anchored at the start and requiring a digit, so an unrelated name like
    # "monday-scans.jpg" can't match. basename() first, so a directory such as
    # /home/mona/ can't either.
    prefix = re.match(r"m([ho])\d", name)
    if prefix:
        return MAXX_HEALTH if prefix.group(1) == "h" else MAXX_ORTHO

    if "health" in name:
        return MAXX_HEALTH
    if "ortho" in name or "orthopedic" in name:
        return MAXX_ORTHO

    # Without reliable logo anchors yet we cannot safely distinguish the two
    # from pixels alone. Default to Maxx Orthopedics (the more common template)
    # so the deterministic path still runs, but record low certainty so the
    # redaction gate can decide. A real anchor matcher replaces this block.
    if img is not None:
        return MAXX_ORTHO
    return UNKNOWN


def is_known(template: str) -> bool:
    return template in _GEOMETRY
