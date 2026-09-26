"""Document text extraction for PDF, DOCX, XLSX, and plain text.

OCR is an optional fallback, not the default path. Extraction status
and errors are tracked per file so downstream consumers know what
succeeded and what needs manual review.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

logger = logging.getLogger("govcon.enrich.extract")


class ExtractionResult:
    __slots__ = ("text", "status", "error", "page_count")

    def __init__(
        self,
        text: str | None,
        status: str,
        error: str | None = None,
        page_count: int | None = None,
    ):
        self.text = text
        self.status = status
        self.error = error
        self.page_count = page_count


def extract_text(data: bytes, mime_type: str, filename: str | None = None) -> ExtractionResult:
    """Extract text from file bytes based on MIME type.

    Returns an ``ExtractionResult`` with status ``success``, ``partial``,
    ``unsupported``, or ``error``.
    """
    mime = (mime_type or "").lower().strip()
    fname = (filename or "").lower()

    if mime == "application/pdf" or fname.endswith(".pdf"):
        return _extract_pdf(data)
    if mime in (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
    ) or fname.endswith(".docx"):
        return _extract_docx(data)
    if mime in (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
    ) or fname.endswith(".xlsx"):
        return _extract_xlsx(data)
    if mime.startswith("text/") or fname.endswith(".txt") or fname.endswith(".csv"):
        return _extract_plain(data)

    return ExtractionResult(None, "unsupported", f"unsupported mime type: {mime}")


def _extract_pdf(data: bytes) -> ExtractionResult:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages: list[str] = []
        for page in reader.pages:
            text = page.extract_text() or ""
            pages.append(text)
        full_text = "\n\n".join(pages)
        if not full_text.strip():
            return ExtractionResult(
                None,
                "partial",
                "PDF contained no extractable text (may need OCR)",
                page_count=len(reader.pages),
            )
        return ExtractionResult(full_text, "success", page_count=len(reader.pages))
    except Exception as exc:
        logger.warning("PDF extraction failed: %s", exc)
        return ExtractionResult(None, "error", str(exc))


def _extract_docx(data: bytes) -> ExtractionResult:
    try:
        from docx import Document

        doc = Document(io.BytesIO(data))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        if not paragraphs:
            return ExtractionResult(None, "partial", "DOCX contained no text paragraphs")
        return ExtractionResult("\n\n".join(paragraphs), "success")
    except Exception as exc:
        logger.warning("DOCX extraction failed: %s", exc)
        return ExtractionResult(None, "error", str(exc))


def _extract_xlsx(data: bytes) -> ExtractionResult:
    try:
        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        parts: list[str] = []
        for sheet in wb.sheetnames:
            ws = wb[sheet]
            rows: list[str] = []
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) if c is not None else "" for c in row]
                if any(cells):
                    rows.append("\t".join(cells))
            if rows:
                parts.append(f"[Sheet: {sheet}]\n" + "\n".join(rows))
        wb.close()
        if not parts:
            return ExtractionResult(None, "partial", "XLSX contained no data")
        return ExtractionResult("\n\n".join(parts), "success")
    except Exception as exc:
        logger.warning("XLSX extraction failed: %s", exc)
        return ExtractionResult(None, "error", str(exc))


def _extract_plain(data: bytes) -> ExtractionResult:
    try:
        for encoding in ("utf-8", "latin-1"):
            try:
                text = data.decode(encoding)
                return ExtractionResult(text, "success")
            except UnicodeDecodeError:
                continue
        return ExtractionResult(None, "error", "unable to decode text file")
    except Exception as exc:
        return ExtractionResult(None, "error", str(exc))


def guess_mime_type(filename: str) -> str:
    """Return a MIME type based on file extension."""
    ext = Path(filename).suffix.lower()
    return {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".doc": "application/msword",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xls": "application/vnd.ms-excel",
        ".txt": "text/plain",
        ".csv": "text/csv",
        ".html": "text/html",
        ".htm": "text/html",
    }.get(ext, "application/octet-stream")
