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
import threading

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


# The ceiling on one rendered page, in pixels.
#
# `scale = dpi / 72` multiplies whatever page size the PDF DECLARES, and that is
# not a number this app controls — a scanner app can emit a page box of 23 x 30
# inches for an ordinary letter-size ticket. At 400 DPI that is 115 MP, a 462 MB
# bitmap, and ~1.2 GB peak to render ONE page. That is what put uvicorn at 5.3 GB
# and had the kernel kill it mid-upload on a 7.9 GB box with no swap.
#
# 30 MP leaves every normal page untouched, so 2.24.0's barcode win is intact:
#   letter 8.5x11 @ 400 DPI = 15 MP      A4 @ 400 DPI = 15.5 MP
#   legal  8.5x14 @ 400 DPI = 19 MP      A3 @ 400 DPI = 31 MP (clamped ~2%)
# Only a page whose declared size is already unreasonable is scaled back, and
# for those the alternative is not a sharper image — it is a dead server.
MAX_RENDER_PIXELS = 30_000_000

# Rendering is the memory-expensive part of this app, and concurrency multiplies
# it. /images ingests up to four uploads in parallel, so four big PDFs rendering
# at once was four times the peak — measured at 1.9 GB for a single ten-page
# file before this module held pages one at a time. One render at a time costs a
# little wall-clock on a multi-PDF upload and takes the multiplier away.
_RENDER_LOCK = threading.Lock()


def _scale_for(page, dpi: float) -> float:
    """The render scale for this page, held under MAX_RENDER_PIXELS.

    Capped on the page's own declared size rather than on the DPI, because the
    DPI is ours and the page size is the file's.
    """
    scale = dpi / 72.0
    try:
        w_pt, h_pt = page.get_size()
    except Exception:          # pragma: no cover - defensive
        return scale
    px = (w_pt * scale) * (h_pt * scale)
    if px > MAX_RENDER_PIXELS and px > 0:
        scale *= (MAX_RENDER_PIXELS / px) ** 0.5
        log.warning("page declares %.0fx%.0f pt; rendering at %.0f DPI instead "
                    "of %.0f to stay under %d MP",
                    w_pt, h_pt, scale * 72, dpi, MAX_RENDER_PIXELS // 1_000_000)
    return scale


def _render_one(page, scale: float) -> bytes:
    """One page -> JPEG bytes, releasing the native bitmap before returning.

    `page.render()` hands back a PdfBitmap wrapping a buffer PDFium allocated.
    Letting it fall out of scope leaves that buffer to the garbage collector,
    which is how ten pages' worth of them were alive at once. `.convert("RGB")`
    has already copied the pixels, so closing it here is safe.
    """
    bitmap = page.render(scale=scale)
    try:
        pil = bitmap.to_pil().convert("RGB")
    finally:
        try:
            bitmap.close()
        except Exception:      # pragma: no cover - older pypdfium2
            pass
    try:
        buf = io.BytesIO()
        pil.save(buf, format="JPEG", quality=95)
        return buf.getvalue()
    finally:
        pil.close()


def _open(data: bytes):
    """(document, page_count), with the page cap applied."""
    pdf = pdfium.PdfDocument(data)
    n = len(pdf)
    if n > MAX_PAGES:
        pdf.close()
        raise ValueError(
            f"PDF has {n} pages (max {MAX_PAGES}); split it into smaller files."
        )
    return pdf, n


def iter_page_images(data: bytes, dpi: int = DEFAULT_DPI):
    """Yield each page as JPEG bytes, one at a time.

    The streaming form, and the one callers should use. Holding every page of a
    multi-page PDF at 400 DPI is what put uvicorn at 5.3 GB and had the kernel
    kill it mid-upload: ten pages measured at 1.9 GB peak, and the cap is fifty.
    Yielding lets the caller store each page and drop it before the next one is
    rendered, so the peak is one page rather than all of them.

    Yields nothing if pypdfium2 is unavailable or the bytes don't parse. Raises
    ValueError if the PDF exceeds MAX_PAGES, so the caller can surface a clear
    per-file error instead of fanning out unboundedly.
    """
    if not (_HAS_PDFIUM and is_pdf(data)):
        return
    pdf, n = _open(data)
    try:
        for i in range(n):
            page = pdf[i]
            try:
                with _RENDER_LOCK:
                    jpeg = _render_one(page, _scale_for(page, dpi))
            finally:
                page.close()
            yield jpeg
            del jpeg
    finally:
        pdf.close()


def render_page(data: bytes, index: int = 0, dpi: int = DEFAULT_DPI) -> bytes | None:
    """One page, rendered on its own.

    For callers that want a single page — the Debug Console traces page 1 and
    used to render the whole document to get it, paying for forty-nine pages it
    then discarded.
    """
    if not (_HAS_PDFIUM and is_pdf(data)):
        return None
    pdf, n = _open(data)
    try:
        if not 0 <= index < n:
            return None
        page = pdf[index]
        try:
            with _RENDER_LOCK:
                return _render_one(page, _scale_for(page, dpi))
        finally:
            page.close()
    finally:
        pdf.close()


def pdf_to_page_images(data: bytes, dpi: int = DEFAULT_DPI) -> list[bytes]:
    """Every page as JPEG bytes, in one list.

    Kept for callers that genuinely want them all at once. Prefer
    `iter_page_images` — this holds the whole document in memory, which is the
    thing that took the server down.
    """
    return list(iter_page_images(data, dpi))
