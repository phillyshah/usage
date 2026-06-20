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


def detect_template(img, filename: str | None = None) -> str:
    """Best-effort template detection.

    Order of evidence:
      1. Filename hint (operators frequently name files by template).
      2. (future) printed-logo anchor match via OpenCV template matching.
    Returns one of MAXX_ORTHO / MAXX_HEALTH / UNKNOWN. UNKNOWN must NOT be
    treated as redactable — callers route it to the manual queue.
    """
    name = (filename or "").lower()
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
