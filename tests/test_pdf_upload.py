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
