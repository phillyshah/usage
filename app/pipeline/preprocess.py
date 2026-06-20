"""Image preprocessing: decode bytes, deskew, denoise, enhance contrast.

All OpenCV use is guarded — if cv2/numpy aren't available the functions degrade
to no-ops so the rest of the pipeline still runs (deterministic barcode/log paths
simply get a less-cleaned image).
"""
from __future__ import annotations

try:
    import cv2
    import numpy as np

    _HAS_CV2 = True
except Exception:  # pragma: no cover - exercised only where cv2 is absent
    cv2 = None  # type: ignore
    np = None  # type: ignore
    _HAS_CV2 = False


def available() -> bool:
    return _HAS_CV2


def decode_image(data: bytes):
    """bytes -> BGR ndarray, or None if cv2 unavailable / undecodable."""
    if not _HAS_CV2:
        return None
    buf = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    return img


def encode_image(img, ext: str = ".jpg") -> bytes:
    if not _HAS_CV2 or img is None:
        return b""
    ok, buf = cv2.imencode(ext, img)
    return buf.tobytes() if ok else b""


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
