"""Roadmap gap 3 (stage 2A/2B): page-level text, OCR and the attachment store (ADR-065)."""

from __future__ import annotations

import io
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.compliance.inventory import build_document_inventory
from govcon.config import Settings
from govcon.enrich.extract import extract_text
from govcon.enrich.ocr import OcrConfig, ocr_available
from govcon.enrich.storage import LocalAttachmentStore
from govcon.models import FilePage, Opportunity, StoredFile
from govcon.security.classification import DataClassification

needs_tesseract = pytest.mark.skipif(not ocr_available(OcrConfig()), reason="Tesseract OCR is not installed")
SCANNED = ("The contractor shall deliver 500 pairs", "of nitrile gloves within 30 days.")


def scanned_pdf(lines=SCANNED) -> bytes:
    """An image-only PDF page: text rendered into a picture, no text layer."""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (1700, 2200), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=56)
    for row, line in enumerate(lines):
        draw.text((120, 200 + 100 * row), line, fill="black", font=font)
    buffer = io.BytesIO()
    image.save(buffer, format="PDF", resolution=200)
    return buffer.getvalue()


def mixed_pdf() -> bytes:
    """Page 1 has a text layer; page 2 is the scanned image."""
    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfgen import canvas

    text_page = io.BytesIO()
    pdf = canvas.Canvas(text_page)
    pdf.drawString(72, 720, "Section L: Offerors shall submit a signed SF 1449 with the quote.")
    pdf.save()
    writer = PdfWriter()
    for source in (text_page.getvalue(), scanned_pdf()):
        for page in PdfReader(io.BytesIO(source)).pages:
            writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


@pytest.fixture()
def session(upgraded_engine):
    with Session(upgraded_engine) as s:
        yield s
        s.rollback()


def _opp(session) -> Opportunity:
    opp = Opportunity(source="sam", source_id=f"ocr-{uuid4().hex}", title="OCR fixture", status="open", raw={}, links={})
    session.add(opp)
    session.flush()
    return opp


# ── extraction ────────────────────────────────────────────────────────────────

def test_without_ocr_an_image_only_pdf_stays_flagged():
    result = extract_text(scanned_pdf(), "application/pdf", "scan.pdf")
    assert result.status == "partial" and "may need OCR" in result.error
    assert [(p.page_no, p.source) for p in result.pages] == [(1, "none")]


def test_unavailable_ocr_reports_why_each_page_is_unreadable():
    result = extract_text(scanned_pdf(), "application/pdf", "scan.pdf",
                          ocr=OcrConfig(tesseract_cmd=str(Path("missing") / "tesseract.exe")))
    assert result.status == "partial" and "not installed" in result.error
    assert result.ocr_failed_pages == [{"page": 1, "reason": "Tesseract OCR is not installed"}]


@needs_tesseract
def test_ocr_reads_an_image_only_page():
    result = extract_text(scanned_pdf(), "application/pdf", "scan.pdf", ocr=OcrConfig())
    assert result.status == "success" and result.ocr_pages == [1] and result.ocr_failed_pages == []
    page = result.pages[0]
    assert page.source == "ocr" and page.confidence and page.confidence > 60
    assert "deliver 500 pairs" in page.text and "nitrile gloves" in page.text


@needs_tesseract
def test_ocr_only_touches_pages_without_a_text_layer():
    result = extract_text(mixed_pdf(), "application/pdf", "mixed.pdf", ocr=OcrConfig())
    assert [(p.page_no, p.source) for p in result.pages] == [(1, "native"), (2, "ocr")]
    assert "SF 1449" in result.pages[0].text and "nitrile gloves" in result.pages[1].text
    assert result.ocr_pages == [2]


def test_sheets_and_documents_become_labelled_parts():
    from openpyxl import Workbook

    workbook = Workbook()
    workbook.active.title = "CLINs"
    workbook.active.append(["0001", "Nitrile gloves", 500])
    workbook.create_sheet("Delivery").append(["FOB", "Destination"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    sheets = extract_text(buffer.getvalue(), "", "pricing.xlsx")
    assert [(p.page_no, p.label) for p in sheets.pages] == [(1, "sheet CLINs"), (2, "sheet Delivery")]
    assert "Nitrile gloves" in sheets.pages[0].text and "Destination" in sheets.pages[1].text
    plain = extract_text(b"Quotes are due Friday.", "text/plain", "note.txt")
    assert [(p.page_no, p.label) for p in plain.pages] == [(1, "document")]


# ── storage ───────────────────────────────────────────────────────────────────

def test_local_store_is_content_addressed_and_never_overwrites(tmp_path):
    import hashlib

    store = LocalAttachmentStore(tmp_path)
    sha = hashlib.sha256(b"alpha").hexdigest()
    first = store.put(b"alpha", sha, 7, "rfq.pdf")
    assert store.put(b"alpha", sha, 7, "rfq.pdf") == first
    Path(first).write_bytes(b"tampered")  # the name is taken by different bytes
    other = store.put(b"alpha", sha, 7, "rfq.pdf")
    assert other != first and store.read(first) == b"tampered" and store.read(other) == b"alpha"
    assert store.read(str(tmp_path / "nope.pdf")) is None
    assert Path(store.put(b"x", "b" * 64, 7, "../../escape.pdf")).parent == (tmp_path / "attachments" / "7").resolve()


# ── persistence and the compliance inventory ─────────────────────────────────

@needs_tesseract
def test_ocr_pages_are_stored_and_satisfy_the_inventory(session, tmp_path):
    from govcon.enrich.attachments import process_local_file

    opp = _opp(session)
    path = tmp_path / "scanned_rfq.pdf"
    path.write_bytes(scanned_pdf())
    row = process_local_file(session, opp, path, classification=DataClassification.PUBLIC, source_origin="test scan")
    pages = session.scalars(select(FilePage).where(FilePage.file_id == row.id)).all()
    assert [(p.page_no, p.text_source) for p in pages] == [(1, "ocr")] and pages[0].char_count > 20
    assert row.page_count == 1 and row.ocr_pages == [1] and row.ocr_failed_pages is None
    inventory, _ = build_document_inventory(session, opp.id)
    assert "unreadable_pages" not in {w.code for w in inventory.warnings}
    [document] = inventory.documents
    assert document.page_texts and "nitrile gloves" in document.page_texts[0]


def test_unread_pages_stay_blocking_with_the_reason(session, tmp_path, monkeypatch):
    from govcon.enrich.attachments import process_local_file

    monkeypatch.setenv("OCR_ENABLED", "false")
    from govcon.config import get_settings
    get_settings.cache_clear()
    opp = _opp(session)
    path = tmp_path / "scanned_rfq.pdf"
    path.write_bytes(mixed_pdf())
    row = process_local_file(session, opp, path, classification=DataClassification.PUBLIC, source_origin="test scan")
    assert row.ocr_pages is None
    inventory, _ = build_document_inventory(session, opp.id)
    warnings = {w.code: w for w in inventory.warnings}
    assert warnings["unreadable_pages"].blocking and "[2]" in warnings["unreadable_pages"].message
    assert not inventory.complete


def test_download_stores_pages(session, tmp_path):
    from govcon.enrich.attachments import download_attachments

    url = "https://files.example.test/rfq.txt"
    opp = _opp(session)
    opp.links = {"attachments": [url]}
    session.flush()
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"Offerors shall include a signed SF 1449.",
                                       headers={"content-type": "text/plain"})))
    [row] = download_attachments(session, opp, settings=Settings(data_dir=tmp_path), client=client,
                                 resolver=lambda host: ["93.184.216.34"])
    pages = session.scalars(select(FilePage).where(FilePage.file_id == row.id)).all()
    assert [(p.page_no, p.label, p.text) for p in pages] == [(1, "document", "Offerors shall include a signed SF 1449.")]
    # A repeat download of the same bytes keeps the one version and its pages.
    [again] = download_attachments(session, opp, settings=Settings(data_dir=tmp_path), client=client,
                                   resolver=lambda host: ["93.184.216.34"])
    assert again.id == row.id
    assert session.scalar(select(StoredFile.page_count).where(StoredFile.id == row.id)) == 1
