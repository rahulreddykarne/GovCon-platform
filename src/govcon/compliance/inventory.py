"""Compliance document inventory (§15.2).

Built before any compliance conclusion. Every source file gets filename, URL,
SHA-256, download time, snapshot/version, document type, page count (when
available), text/table extraction status, and an OCR-needed flag. Missing,
duplicate, re-versioned, amended, failed, and unreadable sources become
visible warnings; a blocking warning makes the run ``incomplete``.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from govcon.compliance.matrix import close_undetected_findings, record_run, upsert_open_finding
from govcon.compliance.records import Inventory, InventoryWarning, SourceDocument
from govcon.models import ComplianceRun, Opportunity, OpportunityEvent, OpportunitySnapshot, StoredFile

INVENTORY_VERSION = "document_inventory.v1"

_AMENDMENT = re.compile(r"\b(?:amendment|amd|mod(?:ification)?)\s*(?:no\.?|number|#)?\s*0*(\d{1,4})\b|\bA0*(\d{1,4})\b(?=[._ -]|$)", re.I)
_SF30 = re.compile(r"\bSF[- ]?30\b|amendment of solicitation", re.I)
_QA = re.compile(r"questions?\s*(?:and|&|/)\s*answers?|\bQ\s*&\s*A\b|\bQ&A\b|\bQandA\b", re.I)
_PRICING = re.compile(r"pric(e|ing)\s*(schedule|sheet|list|workbook)|bid\s*schedule|price\b", re.I)
_FORM = re.compile(r"\b(SF|DD|OF)[- _]?\d{2,4}\b|\bform\b", re.I)
_SOW = re.compile(r"statement\s+of\s+work|performance\s+work\s+statement|\bSOW\b|\bPWS\b|statement\s+of\s+objectives|\bSOO\b", re.I)
_DRAWING = re.compile(r"\bdrawings?\b|\bdwg\b|specification|\bspecs?\b|purchase\s+description", re.I)
_SOLICITATION = re.compile(r"solicitation|\bRFQ\b|\bRFP\b|\bIFB\b|request\s+for\s+(quot|propos)|combined\s+synopsis", re.I)
_ATTACHMENT_REF = re.compile(r"\b(attachment|exhibit|enclosure|appendix)\s+([A-Z]?-?\d{1,3}|[A-Z])\b", re.I)
_DATE = re.compile(
    r"\b((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})\b"
)
_PRECEDENCE = {
    "solicitation": 0, "sow_pws": 0, "attachment": 0, "pricing_sheet": 0, "form": 0,
    "drawing_spec": 0, "qa": 1, "referenced_instruction": 0,
}


def classify_document(filename: str | None, text: str | None, mime_type: str | None = None) -> tuple[str, int | None]:
    """Return ``(document_type, amendment_number)`` from filename first, then the text header."""
    name = filename or ""
    header = (text or "")[:1500]
    for source in (name, header):
        match = _AMENDMENT.search(source)
        if (match and re.search(r"amend|amd|\bA\d", source, re.I)) or _SF30.search(source):
            number = None
            if match:
                number = int(match.group(1) or match.group(2))
            return "amendment", number
        if source is header:
            break
    if _QA.search(name) or _QA.search(header[:300]):
        return "qa", None
    lowered = name.lower()
    if lowered.endswith((".xlsx", ".xls", ".csv")) or _PRICING.search(name):
        return "pricing_sheet", None
    if _SOW.search(name) or _SOW.search(header[:300]):
        return "sow_pws", None
    if _FORM.search(name):
        return "form", None
    if _DRAWING.search(name):
        return "drawing_spec", None
    if _SOLICITATION.search(name) or _SOLICITATION.search(header[:500]):
        return "solicitation", None
    return "attachment", None


def _document_date(text: str | None) -> date | None:
    match = _DATE.search((text or "")[:1500])
    if not match:
        return None
    raw = match.group(1)
    for fmt in ("%B %d, %Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _table_status(doc_mime: str | None, filename: str | None, text: str | None) -> str:
    name = (filename or "").lower()
    mime = (doc_mime or "").lower()
    if name.endswith((".xlsx", ".xls", ".csv")) or "spreadsheet" in mime or mime == "text/csv":
        return "success" if text else "failed"
    if name.endswith(".docx") or "wordprocessingml" in mime:
        if text and "[Table " in text:
            return "success"
        return "none_detected" if text else "failed"
    if name.endswith(".pdf") or mime == "application/pdf":
        return "text_only"
    return "not_applicable"


def document_from_file(row: StoredFile, *, latest_snapshot_id: int | None) -> SourceDocument:
    """Build one inventory entry, re-reading the retained original when available."""
    from govcon.enrich.extract import extract_pdf_pages, extract_text

    text = row.extracted_text
    page_texts: list[str] | None = None
    page_count: int | None = None
    local = Path(row.local_path) if row.local_path else None
    if local is not None and local.is_file():
        data = local.read_bytes()
        name = (row.filename or local.name).lower()
        if name.endswith(".pdf") or (row.mime_type or "") == "application/pdf":
            page_texts = extract_pdf_pages(data)
            page_count = len(page_texts) if page_texts is not None else None
        elif name.endswith(".docx"):
            fresh = extract_text(data, row.mime_type or "", row.filename)
            if fresh.text:
                text = fresh.text
    status = row.extraction_status
    unreadable = [i + 1 for i, page in enumerate(page_texts or []) if not page.strip()]
    ocr_needed = bool(unreadable) or (status == "partial" and "ocr" in (row.extraction_error or "").lower())
    doc_type, amendment_number = classify_document(row.filename, text, row.mime_type)
    rank = 10 + (amendment_number or 0) if doc_type == "amendment" else _PRECEDENCE.get(doc_type, 0)
    return SourceDocument(
        file_id=row.id,
        filename=row.filename,
        url=row.url,
        sha256=row.sha256,
        downloaded_at=row.downloaded_at,
        snapshot_id=row.snapshot_id or latest_snapshot_id,
        document_type=doc_type,
        mime_type=row.mime_type,
        text=text,
        page_texts=page_texts,
        page_count=page_count,
        text_extraction_status=status,
        table_extraction_status=_table_status(row.mime_type, row.filename, text),
        ocr_needed=ocr_needed,
        amendment_number=amendment_number,
        document_date=_document_date(text),
        precedence_rank=rank,
        extraction_error=row.extraction_error if not unreadable else f"unreadable pages: {unreadable}",
    )


def analyze_inventory(
    documents: list[SourceDocument],
    *,
    expected_urls: list[tuple[str, str]] | None = None,
    source_changed_after_download: bool = False,
) -> Inventory:
    """Pure: detect inventory problems for a set of documents."""
    warnings: list[InventoryWarning] = []
    expected: list[dict] = []
    if not documents:
        warnings.append(InventoryWarning("no_source_documents", "critical", "No solicitation source files are available; compliance cannot be established.", [], True))

    for doc in documents:
        status = doc.text_extraction_status
        if status in {"error", "unsupported", None} or (status == "partial" and not doc.ocr_needed):
            warnings.append(InventoryWarning(
                "extraction_failed", "high",
                f"Text extraction failed for {doc.filename or doc.file_id} ({status or 'not extracted'}: {doc.extraction_error or 'no text'}).",
                [doc.file_id], True,
            ))
        if doc.ocr_needed:
            warnings.append(InventoryWarning(
                "unreadable_pages", "high",
                f"{doc.filename or doc.file_id} has unreadable or image-only pages ({doc.extraction_error or 'OCR needed'}); requirements there are not extracted.",
                [doc.file_id], True,
            ))
        if doc.table_extraction_status == "text_only":
            warnings.append(InventoryWarning(
                "tables_flattened", "info",
                f"{doc.filename or doc.file_id}: PDF tables are extracted as flattened text; table-embedded requirements rely on the text scan.",
                [doc.file_id], False,
            ))

    by_hash: dict[str, list[SourceDocument]] = {}
    by_name: dict[str, list[SourceDocument]] = {}
    for doc in documents:
        if doc.sha256:
            by_hash.setdefault(doc.sha256, []).append(doc)
        if doc.filename:
            by_name.setdefault(doc.filename.strip().lower(), []).append(doc)
    for sha, docs in by_hash.items():
        if len(docs) > 1:
            warnings.append(InventoryWarning(
                "duplicate_file", "info",
                f"{len(docs)} files share SHA-256 {sha[:12]}… ({', '.join(d.filename or '?' for d in docs)}).",
                [d.file_id for d in docs], False,
            ))
    for name, docs in by_name.items():
        hashes = {d.sha256 for d in docs}
        if len(hashes) > 1:
            latest = max(docs, key=lambda d: (d.downloaded_at.timestamp() if d.downloaded_at else 0.0, d.file_id or 0))
            warnings.append(InventoryWarning(
                "same_filename_different_hash", "high",
                f"{name} exists in {len(hashes)} versions with different SHA-256; latest download is file {latest.file_id}. Confirm which version controls.",
                [d.file_id for d in docs], False,
            ))

    amendments = sorted((d for d in documents if d.document_type == "amendment"), key=lambda d: d.amendment_number or 0)
    numbers = [d.amendment_number for d in amendments if d.amendment_number]
    if numbers:
        missing = sorted(set(range(1, max(numbers) + 1)) - set(numbers))
        if missing:
            warnings.append(InventoryWarning(
                "amendment_sequence_gap", "high",
                f"Amendment(s) {', '.join(f'{n:04d}' for n in missing)} are missing; latest present is {max(numbers):04d}.",
                [], True,
            ))
        latest = amendments[-1]
        warnings.append(InventoryWarning(
            "newer_amendment", "info",
            f"Latest amendment in the package is "
            f"{f'{latest.amendment_number:04d}' if latest.amendment_number else 'unnumbered'} ({latest.filename}).",
            [latest.file_id], False,
        ))

    if source_changed_after_download:
        warnings.append(InventoryWarning(
            "newer_source_version", "high",
            "The opportunity changed (files/links) after the latest attachment download; re-download before relying on this inventory.",
            [], True,
        ))

    names_lower = " ".join((d.filename or "").lower() for d in documents)
    urls_present = {d.url for d in documents if d.url}
    for url, name in expected_urls or []:
        entry = {"kind": "source_link", "url": url, "name": name, "present": url in urls_present}
        expected.append(entry)
        if not entry["present"]:
            warnings.append(InventoryWarning(
                "missing_expected_attachment", "high",
                f"Listed attachment {name or url} was not downloaded.", [], True,
            ))

    references: dict[str, set] = {}
    for doc in documents:
        for match in _ATTACHMENT_REF.finditer(doc.text or ""):
            label = f"{match.group(1).title()} {match.group(2).upper()}"
            references.setdefault(label, set()).add(doc.file_id)
    headers = " ".join(next((line for line in (d.text or "").splitlines() if line.strip()), "").lower() for d in documents)
    for label, file_ids in sorted(references.items()):
        kind, ident = label.split(" ", 1)
        ident_l = ident.lower()
        patterns = [f"{kind.lower()} {ident_l}", f"{kind.lower()}_{ident_l}", f"{kind.lower()}{ident_l}", f"{kind.lower()}-{ident_l}", f"att{ident_l}", f"att {ident_l}", f"att_{ident_l}"]
        present = any(p in names_lower for p in patterns) or any(p in headers for p in patterns[:1])
        expected.append({"kind": "text_reference", "label": label, "referenced_in": sorted(i for i in file_ids if i is not None), "present": present})
        if not present:
            warnings.append(InventoryWarning(
                "possibly_missing_referenced_attachment", "medium",
                f"{label} is referenced in the source text but no matching file is in the inventory.",
                sorted(i for i in file_ids if i is not None), False,
            ))
    return Inventory(documents=documents, warnings=warnings, expected_attachments=expected)


def inventory_hash(inventory: Inventory) -> str:
    return hashlib.sha256(json.dumps(inventory.content_hash_basis(), default=str).encode()).hexdigest()


def load_inventory(session: Session, opportunity: Opportunity) -> Inventory:
    from govcon.enrich.attachments import _collect_attachment_urls

    latest_snapshot_id = session.scalar(
        select(OpportunitySnapshot.id)
        .where(OpportunitySnapshot.opportunity_id == opportunity.id)
        .order_by(desc(OpportunitySnapshot.fetched_at), desc(OpportunitySnapshot.id))
        .limit(1)
    )
    rows = session.scalars(select(StoredFile).where(StoredFile.opportunity_id == opportunity.id).order_by(StoredFile.id)).all()
    documents = [document_from_file(row, latest_snapshot_id=latest_snapshot_id) for row in rows]
    latest_download = max((r.downloaded_at for r in rows if r.downloaded_at), default=None)
    changed_after = False
    if latest_download is not None:
        changed_after = bool(
            session.scalar(
                select(func.count())
                .select_from(OpportunityEvent)
                .where(
                    OpportunityEvent.opportunity_id == opportunity.id,
                    OpportunityEvent.event_type.in_(["files_added", "links_changed"]),
                    OpportunityEvent.detected_at > latest_download,
                )
            )
        )
    return analyze_inventory(documents, expected_urls=_collect_attachment_urls(opportunity), source_changed_after_download=changed_after)


def build_document_inventory(session: Session, opportunity_id: int) -> tuple[Inventory, ComplianceRun]:
    """Build and persist the inventory; blocking problems also become open findings."""
    opportunity = session.get(Opportunity, opportunity_id)
    if opportunity is None:
        raise ValueError(f"opportunity not found: {opportunity_id}")
    inventory = load_inventory(session, opportunity)
    warnings = [w.as_dict() for w in inventory.warnings]
    snapshot_ids = sorted({d.snapshot_id for d in inventory.documents if d.snapshot_id})
    run = record_run(
        session,
        opportunity_id=opportunity_id,
        run_type="document_inventory",
        run_version=INVENTORY_VERSION,
        output={
            "documents": [d.manifest() for d in inventory.documents],
            "expected_attachments": inventory.expected_attachments,
            "complete": inventory.complete,
        },
        status="complete" if inventory.complete else "incomplete",
        warnings=warnings,
        source_snapshot_ids=snapshot_ids,
        input_hash=inventory_hash(inventory),
    )
    kept: set[int] = set()
    for warning in inventory.warnings:
        if warning.severity == "info":
            continue
        finding = upsert_open_finding(
            session,
            opportunity_id=opportunity_id,
            finding_type=f"inventory_{warning.code}",
            severity="high" if warning.severity in {"critical", "high"} else warning.severity,
            description=warning.message,
            detected_by="document_inventory",
            detector_version=INVENTORY_VERSION,
            source_refs={"file_ids": warning.file_ids},
            blocks_submission=warning.blocking,
            compliance_run_id=run.id,
        )
        kept.add(finding.id)
    close_undetected_findings(session, opportunity_id, detected_by="document_inventory", keep_ids=kept, run_id=run.id)
    return inventory, run
