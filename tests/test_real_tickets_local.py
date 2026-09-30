"""Opt-in end-to-end check against the REAL ticket photos.

These images contain real patient stickers and are deliberately NOT committed
(see .gitignore). To run this locally, drop the real JPEGs or PDFs into
``tests/fixtures/real/`` and run pytest — this test discovers them there. With
no images present (CI, a fresh clone) it skips cleanly, so patient data is
never required by the suite.

This replaces the redaction check that used to live here. There is no longer a
mask to verify, but the harness is worth keeping for what it can still prove
with no API key: that a real photograph ingests without raising, that it is
stored, and — the part that actually matters — that the deterministic barcode
path finds the device labels the photo plainly contains.

That last assertion is the one that would have caught the batch this file was
rewritten for. On that batch every barcode decoded and every handwritten field
came back empty, so the deliverable was 54% red; a harness that only checked
"ingest didn't raise" had nothing to say about it.
"""
from pathlib import Path

import pytest

from app.db import db
from app.pipeline import barcode, preprocess
from app.pipeline.run import _grid_crop, ingest_image
from app.storage import get_object, split_ref

REAL_DIR = Path(__file__).parent / "fixtures" / "real"
IMAGES = sorted(REAL_DIR.glob("*.jp*g")) if REAL_DIR.exists() else []

pytestmark = pytest.mark.skipif(
    not IMAGES, reason="no real ticket photos in tests/fixtures/real/ (opt-in)"
)


@pytest.mark.parametrize("img_path", IMAGES, ids=lambda p: p.name)
def test_a_real_photo_ingests_and_decodes(img_path):
    raw = img_path.read_bytes()
    batch = db.create_batch()
    res = ingest_image(raw, img_path.name, batch["id"])
    assert res["status"] in ("pending_review", "manual_queue")

    ticket = db.get_ticket(res["ticket_id"])
    if res["status"] == "manual_queue":
        assert not ticket.get("source_image_path")
        pytest.fail(f"{img_path.name} could not be read at all")

    ref = ticket.get("source_image_path")
    assert ref, "a pending_review ticket must have a stored image"
    bucket, path = split_ref(ref)
    stored = get_object(bucket, path)
    assert stored, "the stored image must not be empty"

    # The labels are the one thing we can check without an API key.
    img = preprocess.decode_image(stored)
    assert img is not None, "the stored image must decode"
    labels = barcode.drop_junk_labels(
        barcode.decode_region(_grid_crop(img, ticket.get("entity") or "Maxx Orthopedics"))
    )
    assert labels, f"no device labels decoded from {img_path.name}"
