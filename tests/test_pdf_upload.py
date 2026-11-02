"""PDF intake: detection, per-page rasterization, page cap, and the /images
endpoint expanding a multi-page PDF to one ticket PER PAGE."""
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.main import app
from app.pipeline import pdf
from app.pipeline.preprocess import decode_image

client = TestClient(app)


def _pdf(n, size=(1000, 1400)):
    pgs = [Image.new("RGB", size, "white") for _ in range(n)]
    b = io.BytesIO()
    pgs[0].save(b, format="PDF", save_all=True, append_images=pgs[1:])
    return b.getvalue()


def _jpeg():
    b = io.BytesIO()
    Image.new("RGB", (200, 200), "white").save(b, format="JPEG")
    return b.getvalue()


def test_is_pdf():
    assert pdf.is_pdf(_pdf(1)) is True
    assert pdf.is_pdf(_jpeg()) is False
    assert pdf.is_pdf(b"") is False


def test_pdf_to_page_images_one_per_page():
    one = pdf.pdf_to_page_images(_pdf(1))
    assert len(one) == 1
    three = pdf.pdf_to_page_images(_pdf(3))
    assert len(three) == 3
    for page in three:
        assert page[:2] == b"\xff\xd8"           # JPEG magic
        assert decode_image(page) is not None     # decodes through the pipeline


def test_page_count():
    assert pdf.page_count(_pdf(3)) == 3


def test_page_cap_raises(monkeypatch):
    monkeypatch.setattr(pdf, "MAX_PAGES", 2)
    with pytest.raises(ValueError):
        pdf.pdf_to_page_images(_pdf(3))


def test_endpoint_expands_pdf_to_one_ticket_per_page():
    pdf_bytes = _pdf(3)
    r = client.post(
        "/images",
        files=[("files", ("MO9A_B__C.pdf", pdf_bytes, "application/pdf"))],
    )
    assert r.status_code == 202
    body = r.json()
    tickets = body["tickets"]
    assert len(tickets) == 3
    # Filenames are the stem + a per-page suffix; blank pages may land in any of
    # manual_queue/pending_review/error, so we assert on COUNT + names, not status.
    names = [t["filename"] for t in tickets]
    assert names[0].endswith("-p1")
    assert names[1].endswith("-p2")
    assert names[2].endswith("-p3")


def test_bad_pdf_is_isolated_not_a_500():
    r = client.post(
        "/images",
        files=[("files", ("x.pdf", b"%PDF-1.4 broken", "application/pdf"))],
    )
    assert r.status_code == 202
    body = r.json()
    tickets = body["tickets"]
    assert len(tickets) >= 1
    # A garbage PDF that renders no pages surfaces as a per-file error, never a 500.
    assert tickets[0]["status"] == "error"


# ---------------------------------------------------------------------------
# Memory: the thing that took the server down on 2 Nov 2026
#
# Five host OOM kills in twenty minutes, uvicorn at ~5.3 GB each time on a
# 7.9 GB box with no swap, and every request in flight — including an upload
# with nothing to do with it — came back as a 502 from Traefik.
#
# Two causes, both here. `scale = dpi / 72` multiplies whatever page size the
# PDF DECLARES, and a scanner app can declare 23x30 inches for a letter-size
# ticket: 115 MP, a 462 MB bitmap, ~1.2 GB to render ONE page. And every page
# was held in a list until the whole document was rendered.
#
# Measured on a ten-page file with 23x30in pages: 1874 MB before, 386 MB after.
# ---------------------------------------------------------------------------
def _pdf_with_page_size(inches=(8.5, 11.0), n=1, base_dpi=100):
    """A PDF whose DECLARED page box is `inches`.

    PIL applies one `resolution` to both axes, so the pixel dimensions have to
    be proportional to the inches or the declared page is not the one asked
    for — which is the whole variable under test here.
    """
    px = (round(inches[0] * base_dpi), round(inches[1] * base_dpi))
    pgs = [Image.new("RGB", px, "white") for _ in range(n)]
    b = io.BytesIO()
    pgs[0].save(b, format="PDF", save_all=True, append_images=pgs[1:],
                resolution=base_dpi)
    return b.getvalue()


def _size_of(jpeg):
    img = decode_image(jpeg)
    return img.shape[1], img.shape[0]          # (w, h)


def test_a_normal_page_still_renders_at_the_full_dpi():
    """The whole point of 400 DPI (2.24.0) was a DataMatrix code that 200 DPI
    could not resolve. The cap must not quietly undo that."""
    w, h = _size_of(pdf.render_page(_pdf_with_page_size((8.5, 11.0))))
    assert abs(w - 8.5 * pdf.DEFAULT_DPI) <= 2
    assert abs(h - 11.0 * pdf.DEFAULT_DPI) <= 2
    assert w * h < pdf.MAX_RENDER_PIXELS, "a letter page must not even approach the cap"


def test_a_page_that_declares_an_absurd_size_is_capped():
    """23x30in at 400 DPI is 115 MP and ~1.2 GB for one page. The alternative
    to scaling it back is not a sharper image — it is a dead server."""
    w, h = _size_of(pdf.render_page(_pdf_with_page_size((23.6, 30.6))))
    assert w * h <= pdf.MAX_RENDER_PIXELS * 1.02
    assert w > 1000 and h > 1000, "capped, not crushed — it still has to be readable"


def test_the_cap_keeps_the_pages_aspect_ratio():
    w, h = _size_of(pdf.render_page(_pdf_with_page_size((40.0, 20.0))))
    assert abs((w / h) - 2.0) < 0.02


def test_pages_can_be_taken_one_at_a_time():
    """A generator, so a caller stores each page and drops it before the next
    is rendered, instead of holding the whole document."""
    import types

    it = pdf.iter_page_images(_pdf(3))
    assert isinstance(it, types.GeneratorType)
    assert sum(1 for _ in it) == 3


def test_one_page_can_be_rendered_without_rendering_the_rest():
    """The Debug Console traces page 1 and used to render up to fifty pages to
    get it."""
    assert pdf.render_page(_pdf(5), 0)
    assert pdf.render_page(_pdf(5), 4)
    assert pdf.render_page(_pdf(2), 9) is None
    assert pdf.render_page(_jpeg(), 0) is None


def test_the_streaming_form_honours_the_page_cap_too():
    """The cap is the guard against one upload fanning out unboundedly; a
    second way in that skipped it would be no cap at all."""
    with pytest.raises(ValueError):
        list(pdf.iter_page_images(_pdf(pdf.MAX_PAGES + 1)))


def test_only_one_page_is_rendered_at_a_time_across_threads():
    """Four uploads ingest in parallel, so four documents could render at once
    and multiply the peak by four. The lock is what stops that."""
    assert pdf._RENDER_LOCK is not None
    import inspect
    assert "_RENDER_LOCK" in inspect.getsource(pdf.iter_page_images)
    assert "_RENDER_LOCK" in inspect.getsource(pdf.render_page)
