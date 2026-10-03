"""OCR for PDF pages that have no text layer (ADR-065).

Pages are rendered with pypdfium2 and read with Tesseract through
pytesseract. OCR runs locally, so no document content leaves the host.
When the Tesseract binary is missing or a page cannot be read, the page is
reported as failed and stays unreadable downstream; OCR never invents text.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger("govcon.enrich.ocr")

# The UB-Mannheim Windows installer's default location.
_WINDOWS_DEFAULT = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")


@dataclass(frozen=True)
class OcrConfig:
    enabled: bool = True
    tesseract_cmd: str | None = None
    lang: str = "eng"
    dpi: int = 300
    max_pages: int = 300
    min_native_chars: int = 20


@dataclass(frozen=True)
class OcrPage:
    text: str
    confidence: float | None


def ocr_config(settings) -> OcrConfig:
    return OcrConfig(
        enabled=settings.ocr_enabled,
        tesseract_cmd=settings.tesseract_cmd,
        lang=settings.ocr_lang,
        dpi=settings.ocr_dpi,
        max_pages=settings.ocr_max_pages_per_file,
        min_native_chars=settings.ocr_min_native_chars_per_page,
    )


@lru_cache(maxsize=8)
def resolve_tesseract(configured: str | None) -> str | None:
    """Path of the Tesseract binary, or None when it is not installed."""
    if configured:
        return configured if Path(configured).is_file() else None
    found = shutil.which("tesseract")
    if found:
        return found
    if os.name == "nt" and _WINDOWS_DEFAULT.is_file():
        return str(_WINDOWS_DEFAULT)
    return None


def ocr_available(config: OcrConfig) -> bool:
    if not config.enabled:
        return False
    try:
        import pypdfium2  # noqa: F401
        import pytesseract  # noqa: F401
    except ImportError:
        return False
    return resolve_tesseract(config.tesseract_cmd) is not None


def _page_text(data: dict) -> tuple[str, float | None]:
    """Rebuild reading-order lines from ``image_to_data`` output, with mean confidence."""
    lines: dict[tuple[int, int, int], list[str]] = {}
    confidences: list[float] = []
    for i, word in enumerate(data.get("text", [])):
        word = (word or "").strip()
        if not word:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(word)
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            continue
        if conf >= 0:
            confidences.append(conf)
    text = "\n".join(" ".join(words) for _, words in sorted(lines.items()))
    return text, (round(sum(confidences) / len(confidences), 1) if confidences else None)


def ocr_pdf_pages(pdf_bytes: bytes, page_numbers: list[int], config: OcrConfig) -> tuple[dict[int, OcrPage], dict[int, str]]:
    """OCR the given 1-based pages. Returns ``(read, failed)``; ``failed`` maps page to reason."""
    read: dict[int, OcrPage] = {}
    failed: dict[int, str] = {}
    if not page_numbers:
        return read, failed
    if not ocr_available(config):
        reason = "OCR disabled" if not config.enabled else "Tesseract OCR is not installed"
        return read, {page: reason for page in page_numbers}

    import pypdfium2 as pdfium
    import pytesseract

    pytesseract.pytesseract.tesseract_cmd = resolve_tesseract(config.tesseract_cmd)
    allowed = page_numbers[: config.max_pages]
    for page in page_numbers[config.max_pages:]:
        failed[page] = f"over the OCR limit of {config.max_pages} pages per file"
    try:
        document = pdfium.PdfDocument(pdf_bytes)
    except Exception as exc:  # unreadable PDF: nothing can be OCR'd
        return read, {**failed, **{page: f"PDF could not be rendered: {type(exc).__name__}" for page in allowed}}
    try:
        for page_no in allowed:
            try:
                page = document[page_no - 1]
                image = page.render(scale=config.dpi / 72).to_pil()
                data = pytesseract.image_to_data(image, lang=config.lang, output_type=pytesseract.Output.DICT)
                text, confidence = _page_text(data)
            except Exception as exc:
                logger.warning("OCR failed on page %s: %s", page_no, type(exc).__name__)
                failed[page_no] = f"OCR error: {type(exc).__name__}"
                continue
            if text.strip():
                read[page_no] = OcrPage(text=text, confidence=confidence)
            else:
                failed[page_no] = "OCR found no text on the page"
    finally:
        document.close()
    return read, failed
