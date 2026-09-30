"""PDF intake: render each page of an uploaded PDF to an image.

Field operators upload usage tickets as PDFs as well as photos, and a multi-page
PDF carries one ticket per page (different surgeries). We rasterize each page to
a JPEG so it flows through the exact same per-image pipeline as a phone photo
(redact -> store -> decode -> vision).

pypdfium2 ships a self-contained wheel (bundled PDFium, no system/apt deps), so
the import is guarded like barcode.py/preprocess.py — if it's unavailable the
caller degrades gracefully (a PDF simply yields no pages and is reported as a
per-file error rather than crashing the upload).
"""
from __future__ import annotations

import io
import logging

try:
    import pypdfium2 as pdfium

    _HAS_PDFIUM = True
except Exception:  # pragma: no cover - exercised only where the lib is absent
    pdfium = None  # type: ignore
    _HAS_PDFIUM = False

log = logging.getLogger("pipeline.pdf")

# Guard against a mistakenly-huge PDF turning into hundreds of tickets + vision
# calls. Real tickets are a handful of pages.
MAX_PAGES = 50

# 400 DPI, raised from 200 after measuring it. On a real ticket, 200 decoded
# three of four DataMatrix codes and 400 decoded all four: the fourth simply had
# too few pixels per module to be readable, and no amount of post-processing
# recovers detail that was never rendered.
#
# The cost of rendering large is now paid only where it helps. Barcodes get the
# full-resolution page; the vision model gets a copy capped by
# preprocess.for_vision, because every provider downsamples a big image anyway.
DEFAULT_DPI = 400


def available() -> bool:
    return _HAS_PDFIUM


def is_pdf(data: bytes) -> bool:
    """True if the bytes are a PDF (magic header). We key off content, not the
    upload's declared content-type, which renamed/dragged files get wrong."""
    return bool(data) and data[:5] == b"%PDF-"


def page_count(data: bytes) -> int:
    if not (_HAS_PDFIUM and is_pdf(data)):
        return 0
    pdf = pdfium.PdfDocument(data)
    try:
        return len(pdf)
    finally:
        pdf.close()


def pdf_to_page_images(data: bytes, dpi: int = DEFAULT_DPI) -> list[bytes]:
    """Render every page to JPEG bytes (one entry per page).

    Returns [] if pypdfium2 is unavailable or the bytes don't parse. Raises
    ValueError if the PDF exceeds MAX_PAGES so the caller can surface a clear
    per-file error instead of fanning out unboundedly.
    """
    if not (_HAS_PDFIUM and is_pdf(data)):
        return []
    pdf = pdfium.PdfDocument(data)
    try:
        n = len(pdf)
        if n > MAX_PAGES:
            raise ValueError(
                f"PDF has {n} pages (max {MAX_PAGES}); split it into smaller files."
            )
        scale = dpi / 72.0
        out: list[bytes] = []
        for i in range(n):
            page = pdf[i]
            try:
                pil = page.render(scale=scale).to_pil().convert("RGB")
            finally:
                page.close()
            buf = io.BytesIO()
            pil.save(buf, format="JPEG", quality=95)
            out.append(buf.getvalue())
        return out
    finally:
        pdf.close()
