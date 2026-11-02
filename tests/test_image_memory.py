"""An image too big to work on is a problem that comes back every run.

2 Nov 2026, second half. 2.29.0 stopped RENDERING PDF pages at 115 MP, and the
server still died — because the oversized pages were already stored. A batch
killed by the kernel leaves its tickets pending, the next run picks the same
ones up, and three of them through the barcode pass measured 2.1 GB. The loop
had nothing to do with what anyone uploaded next; an upload arriving mid-kill
just inherited the 502.

So the cap belongs where an image is READ, not only where it is written:

    decode one stored 115 MP page      744 MB  ->  253 MB
    three at batch concurrency        2143 MB  ->  608 MB

And it has to happen DURING the decode. Capping afterwards still allocates the
full array first, which is the whole cost — that version measured 2018 MB, a
6% saving on the thing that was killing the box.
"""
import numpy as np
import pytest

from app.pipeline import barcode, preprocess


def _jpeg_of(w, h):
    """A JPEG with real structure, so reduced decoding has something to lose."""
    import cv2

    rng = np.random.default_rng(0)
    small = rng.integers(0, 255, (max(8, h // 40), max(8, w // 40), 3), dtype=np.uint8)
    img = cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR)
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])[1].tobytes()


# ---------------------------------------------------------------------------
# cap_pixels
# ---------------------------------------------------------------------------
def test_a_real_ticket_photo_is_never_touched():
    """Barcodes want every pixel. This is a survival limit, not tidiness — if
    it fires on anything anyone actually photographs it is set wrong."""
    for shape in [(3000, 2250, 3),       # the phone photo that prompted this
                  (4400, 3400, 3),       # a letter page rendered at 400 DPI
                  (6000, 4000, 3)]:      # a 24 MP camera
        img = np.zeros(shape, np.uint8)
        assert preprocess.cap_pixels(img) is img, f"{shape} should pass untouched"


def test_an_oversized_image_is_brought_under_the_limit():
    out = preprocess.cap_pixels(np.zeros((12222, 9444, 3), np.uint8))
    assert out.shape[0] * out.shape[1] <= preprocess.MAX_IMAGE_PIXELS


def test_the_cap_keeps_the_aspect_ratio():
    out = preprocess.cap_pixels(np.zeros((12000, 6000, 3), np.uint8))
    assert abs((out.shape[1] / out.shape[0]) - 0.5) < 0.01


def test_cap_pixels_tolerates_no_image():
    assert preprocess.cap_pixels(None) is None


# ---------------------------------------------------------------------------
# Decoding small, rather than decoding then shrinking
# ---------------------------------------------------------------------------
def test_an_oversized_jpeg_is_decoded_small():
    """The measurement that matters: capping AFTER a full decode saved 6%.
    Decoding at a reduced scale saved 66%, because the big array never exists."""
    data = _jpeg_of(9444, 12222)                      # 115 MP
    img = preprocess.decode_image(data, max_px=preprocess.MAX_IMAGE_PIXELS)
    assert img.shape[0] * img.shape[1] <= preprocess.MAX_IMAGE_PIXELS
    assert img.shape[0] > 2000 and img.shape[1] > 2000, "reduced, not destroyed"


def test_a_normal_jpeg_is_decoded_at_full_size():
    data = _jpeg_of(2250, 3000)
    plain = preprocess.decode_image(data)
    capped = preprocess.decode_image(data, max_px=preprocess.MAX_IMAGE_PIXELS)
    assert capped.shape == plain.shape


def test_decoding_without_a_limit_is_unchanged():
    """Every other caller — the vision copy, the tests, the Expiry Log — keeps
    the behaviour it had."""
    data = _jpeg_of(9444, 12222)
    assert preprocess.decode_image(data).shape[:2] == (12222, 9444)


def test_undecodable_bytes_still_return_none():
    assert preprocess.decode_image(b"not an image", max_px=1_000) is None


def test_the_barcodes_still_decode_after_the_cap():
    """The cap must not cost a label. Measured on a real 115 MP page: 5 codes
    at full size, 6 after the cap — different scales catch different codes,
    which is the same thing 2.24.0 found when it pooled two passes."""
    import cv2

    from tests.fixtures._synthetic import gs1_payload, gtin14, make_ticket_image

    payload = gs1_payload(gtin14("0810008121893"), "270331", "P04122711")
    ticket = cv2.imdecode(
        np.frombuffer(make_ticket_image(payload), np.uint8), cv2.IMREAD_COLOR)
    big = cv2.resize(ticket, (9444, 12222), interpolation=cv2.INTER_LINEAR)
    data = cv2.imencode(".jpg", big, [cv2.IMWRITE_JPEG_QUALITY, 95])[1].tobytes()

    full = barcode.decode_region(preprocess.decode_image(data))
    capped = barcode.decode_region(
        preprocess.decode_image(data, max_px=preprocess.MAX_IMAGE_PIXELS))
    assert len(capped) >= len(full), "the cap must not cost a label"
    assert len(capped) >= 1


# ---------------------------------------------------------------------------
# Both ends of the pipeline
# ---------------------------------------------------------------------------
def test_both_the_write_and_the_read_side_apply_the_limit():
    """2.29.0 capped only what was WRITTEN, and the already-stored pages kept
    killing the box. Either side alone leaves the failure in place."""
    import inspect

    from app.pipeline import run

    assert "max_px" in inspect.getsource(run.ingest_image)
    assert "max_px" in inspect.getsource(run.process_ticket)
