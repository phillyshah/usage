"""Image preprocessing: decode bytes, deskew, denoise, enhance contrast.

All OpenCV use is guarded — if cv2/numpy aren't available the functions degrade
to no-ops so the rest of the pipeline still runs (deterministic barcode/log paths
simply get a less-cleaned image).
"""
from __future__ import annotations

import logging

try:
    import cv2
    import numpy as np

    _HAS_CV2 = True
except Exception:  # pragma: no cover - exercised only where cv2 is absent
    cv2 = None  # type: ignore
    np = None  # type: ignore
    _HAS_CV2 = False

log = logging.getLogger("pipeline.preprocess")


def available() -> bool:
    return _HAS_CV2


def decode_image(data: bytes, max_px: int | None = None):
    """bytes -> BGR ndarray, or None if cv2 unavailable / undecodable.

    With `max_px`, an oversized image is decoded SMALL rather than decoded and
    then shrunk. That distinction is the whole point: capping afterwards still
    materialises the full array first, so a 115 MP page still costs its 345 MB
    before anything is saved. libjpeg can scale during decoding, which cv2
    exposes as IMREAD_REDUCED_COLOR_N, so the big array is never allocated.
    """
    if not _HAS_CV2:
        return None
    buf = np.frombuffer(data, dtype=np.uint8)
    if max_px:
        img = _decode_within(buf, max_px)
        if img is not None:
            return img
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def _decode_within(buf, max_px: int):
    """Decode at the smallest reduction that fits `max_px`, or None to fall back.

    The 1/8 probe is cheap — a 115 MP page comes back as 1.8 MP — and it is the
    only way to learn an image's true size without decoding it whole.
    """
    reduced = {2: cv2.IMREAD_REDUCED_COLOR_2,
               4: cv2.IMREAD_REDUCED_COLOR_4,
               8: cv2.IMREAD_REDUCED_COLOR_8}
    try:
        probe = cv2.imdecode(buf, cv2.IMREAD_REDUCED_COLOR_8)
        if probe is None:
            return None
        h, w = probe.shape[:2]
        full_px = (h * 8) * (w * 8)
        if full_px <= max_px:
            return None                      # normal size: decode it properly
        for n in (2, 4, 8):
            if full_px / float(n * n) <= max_px:
                img = cv2.imdecode(buf, reduced[n])
                if img is None:
                    return None
                log.warning("image is ~%.0f MP; decoded at 1/%d (%.0f MP) to "
                            "stay within memory", full_px / 1e6, n,
                            (img.shape[0] * img.shape[1]) / 1e6)
                return cap_pixels(img, max_px)
        log.warning("image is ~%.0f MP; even 1/8 is over the limit", full_px / 1e6)
        return cap_pixels(probe, max_px)
    except Exception as e:                   # pragma: no cover - defensive
        log.warning("reduced decode failed (%s); falling back to a full decode", e)
        return None


def encode_image(img, ext: str = ".jpg") -> bytes:
    if not _HAS_CV2 or img is None:
        return b""
    ok, buf = cv2.imencode(ext, img)
    return buf.tobytes() if ok else b""


# The ceiling on any image this pipeline will work on, in pixels.
#
# Barcodes want every pixel, so nothing here is capped for tidiness — this is a
# survival limit. A stored page decodes to 3 bytes per pixel and the batch runs
# three tickets at a time, so the arithmetic is unforgiving:
#
#   a letter page @ 400 DPI   15 MP ->  45 MB decoded   (untouched)
#   a 24 MP phone photo       24 MP ->  72 MB decoded   (untouched)
#   a 23x30in page @ 400 DPI 115 MP -> 345 MB decoded, and 2.1 GB measured for
#                                      three of them through the barcode pass
#
# 2.29.0 stopped RENDERING pages that large. It did nothing for the ones already
# stored, which keep coming back as pending tickets on every batch run — so the
# cap has to be applied where an image is READ as well as where it is written.
#
# 30 MP leaves every real ticket alone, phone photos included.
MAX_IMAGE_PIXELS = 30_000_000


def cap_pixels(img, max_px: int = MAX_IMAGE_PIXELS):
    """Scale an image down to `max_px`, keeping its aspect ratio.

    Returns the image unchanged when it is already within the limit, which is
    the case for every ticket anyone actually photographs.
    """
    if img is None:
        return img
    h, w = img.shape[:2]
    px = h * w
    if px <= max_px or px == 0:
        return img
    import cv2

    scale = (max_px / float(px)) ** 0.5
    out = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))),
                     interpolation=cv2.INTER_AREA)
    log.warning("image is %.0f MP (%dx%d); working at %.0f MP to stay within "
                "memory", px / 1e6, w, h, (out.shape[0] * out.shape[1]) / 1e6)
    return out


# What the vision models actually use. Every one of them downsamples a large
# image server-side before looking at it, so uploading a 15 MP page buys no
# accuracy — it buys upload time and, on some providers, tokens. Barcodes are
# the opposite: they need every pixel we can give them. One stored image now
# serves both, at full resolution, and this caps only the copy sent to the
# model.
VISION_MAX_EDGE = 1600


def for_vision(img, ext: str = ".jpg") -> bytes:
    """The image as the model should receive it: long edge capped, re-encoded.

    Never upscales — a small photo is sent as-is.
    """
    if img is None:
        return b""
    h, w = img.shape[:2]
    longest = max(h, w)
    if longest > VISION_MAX_EDGE:
        import cv2

        scale = VISION_MAX_EDGE / float(longest)
        img = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))),
                         interpolation=cv2.INTER_AREA)
    return encode_image(img, ext)


def _deskew(gray):
    """Estimate dominant text skew via minAreaRect over thresholded ink."""
    thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    coords = cv2.findNonZero(thresh)
    if coords is None:
        return 0.0
    angle = cv2.minAreaRect(coords)[-1]
    if angle < -45:
        angle = 90 + angle
    # Only correct meaningful skew; ignore tiny jitter.
    return angle if abs(angle) > 0.75 else 0.0


def preprocess(img):
    """Deskew + denoise + contrast-normalize. Returns a cleaned BGR image.

    Safe on None input (returns None). Never raises on a bad photo — the worst
    case is we return the original image unchanged.
    """
    if not _HAS_CV2 or img is None:
        return img
    try:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        angle = _deskew(gray)
        if angle:
            h, w = img.shape[:2]
            m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
            img = cv2.warpAffine(
                img, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE
            )
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        # Denoise then CLAHE for local contrast (helps glare/uneven lighting).
        gray = cv2.fastNlMeansDenoising(gray, h=10)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        gray = clahe.apply(gray)
        return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    except Exception:
        return img
