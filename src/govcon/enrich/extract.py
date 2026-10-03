"""Document text extraction for PDF, DOCX, XLSX, and plain text.

OCR is an optional fallback, not the default path. Extraction status
and errors are tracked per file so downstream consumers know what
succeeded and what needs manual review.
"""

from __future__ import annotations

import io
import logging
import zipfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("govcon.enrich.extract")


@dataclass(frozen=True)
class ExtractionLimits:
    """Bounds that keep a hostile or malformed file from exhausting the host."""

    max_pdf_pages: int = 1500
    max_text_chars: int = 5_000_000
    max_zip_members: int = 5_000
    max_zip_uncompressed_bytes: int = 300 * 1024 * 1024
    max_zip_compression_ratio: float = 200.0
    max_xlsx_sheets: int = 50
    max_xlsx_cells: int = 500_000


DEFAULT_LIMITS = ExtractionLimits()


class ExtractionLimitExceeded(ValueError):
    """The file is rejected before parsing (archive bomb or similar)."""


def check_zip_container(data: bytes, limits: ExtractionLimits = DEFAULT_LIMITS) -> None:
    """Reject DOCX/XLSX (ZIP) containers that would expand unreasonably."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ExtractionLimitExceeded(f"not a valid ZIP container: {exc}") from exc
    with archive:
        members = archive.infolist()
        if len(members) > limits.max_zip_members:
            raise ExtractionLimitExceeded(f"{len(members)} archive members exceed the {limits.max_zip_members} limit")
        total = 0
        for member in members:
            total += member.file_size
            if member.compress_size > 0 and member.file_size / member.compress_size > limits.max_zip_compression_ratio:
                raise ExtractionLimitExceeded(f"member {member.filename!r} has a suspicious compression ratio")
            if member.compress_size == 0 and member.file_size > 0:
                raise ExtractionLimitExceeded(f"member {member.filename!r} has a suspicious compression ratio")
        if total > limits.max_zip_uncompressed_bytes:
            raise ExtractionLimitExceeded(f"archive expands to {total} bytes, over the limit")


def _cap_text(result: "ExtractionResult", limits: ExtractionLimits) -> "ExtractionResult":
    if result.text is not None and len(result.text) > limits.max_text_chars:
        note = f"text truncated at {limits.max_text_chars} characters"
        return ExtractionResult(
            result.text[: limits.max_text_chars],
            "partial",
            f"{result.error}; {note}" if result.error else note,
            page_count=result.page_count,
        )
    return result


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


def extract_text(
    data: bytes,
    mime_type: str,
    filename: str | None = None,
    limits: ExtractionLimits = DEFAULT_LIMITS,
) -> ExtractionResult:
    """Extract text from file bytes based on MIME type.

    Returns an ``ExtractionResult`` with status ``success``, ``partial``,
    ``unsupported``, or ``error``. ``limits`` bound pages, archive expansion,
    workbook size, and returned text; truncation yields ``partial``.
    """
    mime = (mime_type or "").lower().split(";", 1)[0].strip()
    fname = (filename or "").lower()

    if mime == "application/pdf" or fname.endswith(".pdf"):
        return _cap_text(_extract_pdf(data, limits), limits)
    if mime in (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
    ) or fname.endswith(".docx"):
        return _cap_text(_extract_docx(data, limits), limits)
    if mime in (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
    ) or fname.endswith(".xlsx"):
        return _cap_text(_extract_xlsx(data, limits), limits)
    if mime.startswith("text/") or fname.endswith(".txt") or fname.endswith(".csv"):
        return _cap_text(_extract_plain(data), limits)

    return ExtractionResult(None, "unsupported", f"unsupported mime type: {mime}")


def _extract_pdf(data: bytes, limits: ExtractionLimits = DEFAULT_LIMITS) -> ExtractionResult:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        total_pages = len(reader.pages)
        pages: list[str] = []
        for index, page in enumerate(reader.pages):
            if index >= limits.max_pdf_pages:
                break
            text = page.extract_text() or ""
            pages.append(text)
        full_text = "\n\n".join(pages)
        truncated = total_pages > limits.max_pdf_pages
        if not full_text.strip():
            return ExtractionResult(
                None,
                "partial",
                "PDF contained no extractable text (may need OCR)",
                page_count=total_pages,
            )
        if truncated:
            return ExtractionResult(
                full_text,
                "partial",
                f"PDF truncated at {limits.max_pdf_pages} of {total_pages} pages",
                page_count=total_pages,
            )
        return ExtractionResult(full_text, "success", page_count=total_pages)
    except Exception as exc:
        logger.warning("PDF extraction failed: %s", exc)
        return ExtractionResult(None, "error", str(exc))


def extract_pdf_pages(data: bytes, limits: ExtractionLimits = DEFAULT_LIMITS) -> list[str] | None:
    """Return per-page text for a PDF (at most ``max_pdf_pages``), or ``None`` when unreadable."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return [page.extract_text() or "" for _, page in zip(range(limits.max_pdf_pages), reader.pages)]
    except Exception as exc:
        logger.warning("PDF page extraction failed: %s", exc)
        return None


def _extract_docx(data: bytes, limits: ExtractionLimits = DEFAULT_LIMITS) -> ExtractionResult:
    try:
        check_zip_container(data, limits)
    except ExtractionLimitExceeded as exc:
        return ExtractionResult(None, "error", f"rejected before parsing: {exc}")
    try:
        from docx import Document

        doc = Document(io.BytesIO(data))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        # Requirements are often embedded in tables (packaging, CLIN, marking);
        # paragraph-only extraction silently drops them.
        tables: list[str] = []
        for index, table in enumerate(doc.tables, start=1):
            rows = []
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells]
                if any(cells):
                    rows.append("\t".join(cells))
            if rows:
                tables.append(f"[Table {index}]\n" + "\n".join(rows))
        if not paragraphs and not tables:
            return ExtractionResult(None, "partial", "DOCX contained no text paragraphs or tables")
        return ExtractionResult("\n\n".join(paragraphs + tables), "success")
    except Exception as exc:
        logger.warning("DOCX extraction failed: %s", exc)
        return ExtractionResult(None, "error", str(exc))


def _extract_xlsx(data: bytes, limits: ExtractionLimits = DEFAULT_LIMITS) -> ExtractionResult:
    try:
        check_zip_container(data, limits)
    except ExtractionLimitExceeded as exc:
        return ExtractionResult(None, "error", f"rejected before parsing: {exc}")
    try:
        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        parts: list[str] = []
        notes: list[str] = []
        cells_seen = 0
        sheet_names = list(wb.sheetnames)
        if len(sheet_names) > limits.max_xlsx_sheets:
            notes.append(f"only the first {limits.max_xlsx_sheets} of {len(sheet_names)} sheets were read")
            sheet_names = sheet_names[: limits.max_xlsx_sheets]
        for sheet in sheet_names:
            ws = wb[sheet]
            rows: list[str] = []
            for row in ws.iter_rows(values_only=True):
                cells_seen += len(row)
                if cells_seen > limits.max_xlsx_cells:
                    notes.append(f"stopped after {limits.max_xlsx_cells} cells")
                    break
                cells = [str(c) if c is not None else "" for c in row]
                if any(cells):
                    rows.append("\t".join(cells))
            if rows:
                parts.append(f"[Sheet: {sheet}]\n" + "\n".join(rows))
            if cells_seen > limits.max_xlsx_cells:
                break
        wb.close()
        if not parts:
            return ExtractionResult(None, "partial", "XLSX contained no data")
        if notes:
            return ExtractionResult("\n\n".join(parts), "partial", "; ".join(notes))
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
