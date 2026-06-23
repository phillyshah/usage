"""Opt-in PHI redaction check against the REAL ticket photos.

These images contain real patient stickers and are deliberately NOT committed
(see .gitignore / BUILD_SPEC §9). To verify redaction locally, drop the real
JPEGs into ``tests/fixtures/real/`` and run pytest — this test discovers them
there. With no images present (CI, a fresh clone) it skips cleanly, so patient
data is never required by the suite.

What it asserts per image:
  * ingest returns a status (pending_review or manual_queue) and never raises;
  * the PHI gate holds: a redacted image is stored ONLY when the patient region
    was located, and the stored redacted bytes differ from the raw upload (i.e.
    something was actually masked) — and no raw image is ever persisted.
"""
from pathlib import Path

import pytest

from app.db import db
from app.pipeline.run import ingest_image
from app.storage import get_object, split_ref

REAL_DIR = Path(__file__).parent / "fixtures" / "real"
IMAGES = sorted(REAL_DIR.glob("*.jp*g")) if REAL_DIR.exists() else []

pytestmark = pytest.mark.skipif(
    not IMAGES, reason="no real ticket photos in tests/fixtures/real/ (PHI, opt-in)"
)


@pytest.mark.parametrize("img_path", IMAGES, ids=lambda p: p.name)
def test_patient_region_redacted(img_path):
    raw = img_path.read_bytes()
    batch = db.create_batch()
    res = ingest_image(raw, img_path.name, batch["id"])
    assert res["status"] in ("pending_review", "manual_queue")

    ticket = db.get_ticket(res["ticket_id"])
    if res["status"] == "manual_queue":
        # Fail-safe: region not located -> nothing stored, routed for review.
        assert not ticket.get("source_image_path")
        return

    # Redaction located -> only the redacted image is stored, and it differs from
    # the raw upload (the patient region was masked, not passed through verbatim).
    ref = ticket.get("source_image_path")
    assert ref, "a pending_review ticket must have a stored redacted image"
    bucket, path = split_ref(ref)
    stored = get_object(bucket, path)
    assert stored and stored != raw, "stored image must be redacted, not the raw photo"
