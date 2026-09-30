"""Finding the device labels on a page that isn't the Maxx ticket.

Distributors use their own forms. One arrived as a photo inside a PDF and
produced ZERO lines: rendered at 200 DPI its DataMatrix codes had too few
pixels per module to decode, and the grid crop was tuned to a layout it does
not share.

Measured on the two real files, before and after:

    Maxx ticket (MO18806-A)   3 of 4 labels  ->  4 of 4
    Distributor form           0 of 4        ->  3 of 4

Three changes got that, none of which cost anything per ticket:

  * render PDFs at 400 DPI, because no post-processing recovers detail that was
    never rendered;
  * pool the grid crop and a whole-page pass, because neither dominates;
  * fall through other decoder settings when a pass finds nothing at all.

The fourth label on the distributor form still does not decode. It reaches the
workbook as a vision-only line instead, which is the designed fallback.
"""
import numpy as np

from app.pipeline import barcode, pdf, preprocess


def _label(gtin=None, lot=None, raw=None, ref=None):
    return {"gtin": gtin, "lot": lot, "raw": raw, "ref": ref, "decoded": bool(gtin)}


# ---------------------------------------------------------------------------
# Pooling the passes
# ---------------------------------------------------------------------------
def test_the_same_label_seen_by_both_passes_becomes_one_line():
    """Two passes over one page see the same sticker twice. If that became two
    lines, every pooled ticket would double-count its implants."""
    a = [_label(gtin="00811767021913", lot="U28052710", ref="UPUUX834-K")]
    b = [_label(gtin="00811767021913", lot="U28052710", ref="UPUUX834-K")]
    assert len(barcode.merge_labels(a, b)) == 1


def test_a_label_only_one_pass_found_is_kept():
    """The whole point: the crop finds codes the page misses and vice versa."""
    crop = [_label(gtin="1", lot="A")]
    page = [_label(gtin="1", lot="A"), _label(gtin="2", lot="B")]
    merged = barcode.merge_labels(crop, page)
    assert {l["lot"] for l in merged} == {"A", "B"}


def test_the_same_part_with_different_lots_stays_two_lines():
    """Two of the same component really are two implants."""
    merged = barcode.merge_labels(
        [_label(gtin="1", lot="A")], [_label(gtin="1", lot="B")])
    assert len(merged) == 2


def test_the_first_sighting_wins():
    first = _label(gtin="1", lot="A", ref="FROM-CROP")
    second = _label(gtin="1", lot="A", ref="FROM-PAGE")
    assert barcode.merge_labels([first], [second])[0]["ref"] == "FROM-CROP"


def test_undecoded_payloads_are_deduped_on_their_raw_text():
    """Anything carrying no GS1 data has no (gtin, lot) to key on."""
    merged = barcode.merge_labels([_label(raw="JUNK")], [_label(raw="JUNK")])
    assert len(merged) == 1


def test_empty_and_missing_passes_are_harmless():
    assert barcode.merge_labels([], None) == []
    assert len(barcode.merge_labels([_label(gtin="1", lot="A")], [])) == 1


# ---------------------------------------------------------------------------
# Resolution: render big for barcodes, send small to the model
# ---------------------------------------------------------------------------
def test_pdfs_render_at_enough_resolution_to_decode():
    """200 DPI lost a label on a real ticket that 400 DPI read."""
    assert pdf.DEFAULT_DPI >= 400


def test_the_vision_copy_is_capped():
    """Every provider downsamples a large image anyway, so uploading the full
    page buys no accuracy — it buys upload time and tokens."""
    page = np.full((4400, 3351, 3), 255, np.uint8)
    capped = preprocess.decode_image(preprocess.for_vision(page))
    assert max(capped.shape[:2]) <= preprocess.VISION_MAX_EDGE
    assert len(preprocess.for_vision(page)) < len(preprocess.encode_image(page))


def test_a_small_photo_is_never_upscaled():
    """Inventing pixels helps nothing and costs tokens."""
    small = np.full((400, 600, 3), 255, np.uint8)
    out = preprocess.decode_image(preprocess.for_vision(small))
    assert out.shape[:2] == (400, 600)


def test_the_barcode_pass_still_gets_the_full_resolution_image():
    """The capping must apply ONLY to the vision copy — barcodes need every
    pixel, which is the whole reason the render DPI went up."""
    import inspect

    from app.pipeline import run

    src = inspect.getsource(run.process_ticket)
    vision_at = src.index("for_vision")
    decode_at = src.index("decode_region")
    assert decode_at < vision_at, "barcodes must decode before the image is capped"


def test_the_decoder_falls_through_other_settings_on_a_miss():
    assert len(barcode._SHRINK_LADDER) > 1
