"""Template detection from the filename.

This is a PHI gate, not just an accuracy knob: the two layouts are mirror
images (Health's patient sticker at x=0.04, Orthopedics' at x=0.55), so a
Health ticket detected as Orthopedics masks the empty right-hand side and
leaves the patient sticker fully visible in the stored image.

The production naming convention is an entity prefix plus a ticket number —
"MH17469.jpg", "MO083596.jpg" — which the original word-only matcher ("health"
/ "ortho" anywhere in the name) never recognized, so every Maxx Health ticket
was being read as Maxx Orthopedics.
"""
import numpy as np
import pytest

from app.pipeline.template import (
    MAXX_HEALTH,
    MAXX_ORTHO,
    detect_template,
    geometry_for,
)

_IMG = np.zeros((400, 600, 3), np.uint8)


@pytest.mark.parametrize("filename", [
    "MH17469.jpg",
    "MH99999-A.jpeg",
    "mh17469.jpg",          # lower case
    "MH17469-p2",           # page of a multi-page PDF (no extension)
    "/uploads/MH555.jpg",   # full path
    "/home/mona/MH555.jpg",  # a directory containing "mo" must not win
    "maxx-health-01.jpg",   # descriptive name still works
])
def test_health_tickets_detect_as_health(filename):
    assert detect_template(_IMG, filename) == MAXX_HEALTH


@pytest.mark.parametrize("filename", [
    "MO083596.jpg",
    "MO12345-p1",
    "mo083596.jpeg",
    "ortho-sample.png",
    "maxx-orthopedics.jpg",
])
def test_ortho_tickets_detect_as_ortho(filename):
    assert detect_template(_IMG, filename) == MAXX_ORTHO


def test_health_and_ortho_mask_opposite_corners():
    """The whole reason detection matters — confirm the regions really do differ."""
    health = geometry_for(detect_template(_IMG, "MH17469.jpg")).patient_region
    ortho = geometry_for(detect_template(_IMG, "MO083596.jpg")).patient_region
    assert health.x != ortho.x
    assert health.x < 0.5 < ortho.x


@pytest.mark.parametrize("filename", [
    "monday-scans.jpg",   # starts with "mo" but no digit follows
    "motion-study.jpg",
    "mother.png",
    "MHX-no-digit.jpg",
])
def test_prefix_requires_a_digit_so_unrelated_names_dont_match(filename):
    """These must not be *claimed* by the prefix rule. They fall through to the
    existing best-effort guess, which is unchanged by this fix."""
    from app.pipeline import template

    name = template.os.path.basename(filename).lower()
    assert template.re.match(r"m([ho])\d", name) is None
