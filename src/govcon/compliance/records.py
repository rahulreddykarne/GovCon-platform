"""Plain records shared by the pure compliance stages.

The extraction, reconciliation, conflict, and validation logic operates on
these records so the regression benchmark can run the same code path without
a database. The DB orchestration in each module maps them to ORM rows.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any

REQUIREMENT_TYPES = (
    "administrative", "technical", "pricing", "delivery", "past_performance", "certification",
    "representation", "set_aside", "country_of_origin", "cybersecurity", "formatting", "page_limit",
    "signature", "amendment_acknowledgment", "submission", "other",
)
SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
ARTIFACT_TYPES = frozenset({"signature", "amendment_acknowledgment", "submission", "administrative", "formatting", "page_limit"})
RESOLVED_STATUSES = frozenset({"satisfied", "not_applicable", "superseded"})
UNRESOLVED_STATUSES = frozenset({"unreviewed", "missing", "unknown", "needs_review", "stale"})


@dataclass
class SourceDocument:
    """One inventory entry (§15.2)."""

    file_id: int | None
    filename: str | None
    url: str | None
    sha256: str | None
    downloaded_at: datetime | None
    snapshot_id: int | None
    document_type: str
    mime_type: str | None
    text: str | None
    page_texts: list[str] | None = None
    page_count: int | None = None
    text_extraction_status: str | None = None
    table_extraction_status: str | None = None
    ocr_needed: bool = False
    amendment_number: int | None = None
    document_date: date | None = None
    precedence_rank: int = 0
    extraction_error: str | None = None

    def pages(self) -> list[tuple[int | None, str]]:
        if self.page_texts:
            return [(index + 1, text) for index, text in enumerate(self.page_texts)]
        return [(None, self.text or "")]

    def page_text(self, page: int | None) -> str | None:
        if page is not None and self.page_texts and 1 <= page <= len(self.page_texts):
            return self.page_texts[page - 1]
        return self.text

    def manifest(self) -> dict[str, Any]:
        return {
            "file_id": self.file_id,
            "filename": self.filename,
            "url": self.url,
            "sha256": self.sha256,
            "downloaded_at": self.downloaded_at.isoformat() if self.downloaded_at else None,
            "snapshot_id": self.snapshot_id,
            "document_type": self.document_type,
            "mime_type": self.mime_type,
            "page_count": self.page_count,
            "text_extraction_status": self.text_extraction_status,
            "table_extraction_status": self.table_extraction_status,
            "ocr_needed": self.ocr_needed,
            "amendment_number": self.amendment_number,
            "document_date": self.document_date.isoformat() if self.document_date else None,
            "precedence_rank": self.precedence_rank,
        }


@dataclass
class InventoryWarning:
    code: str
    severity: str
    message: str
    file_ids: list[int | None] = field(default_factory=list)
    blocking: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Inventory:
    documents: list[SourceDocument]
    warnings: list[InventoryWarning]
    expected_attachments: list[dict[str, Any]] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not any(w.blocking for w in self.warnings)

    def by_file_id(self) -> dict[int | None, SourceDocument]:
        return {doc.file_id: doc for doc in self.documents}

    def amendments(self) -> list[SourceDocument]:
        return sorted(
            (d for d in self.documents if d.document_type == "amendment"),
            key=lambda d: (d.amendment_number or 0, d.document_date or date.min),
        )

    def content_hash_basis(self) -> list[tuple[Any, Any]]:
        return sorted((d.file_id or 0, d.sha256 or "") for d in self.documents)


@dataclass
class Candidate:
    """One requirement proposed by one extraction pass."""

    candidate_id: str
    pass_label: str
    requirement_text: str
    requirement_type: str | None = None
    mandatory: bool | None = None
    severity: str | None = None
    response_required: bool | None = None
    source_file_id: int | None = None
    source_page: int | None = None
    source_section: str | None = None
    supporting_quote: str | None = None
    source_snapshot_id: int | None = None
    confidence: float | None = None
    uncertainty_reason: str | None = None
    key_values: dict[str, Any] = field(default_factory=dict)
    clause_refs: list[str] = field(default_factory=list)
    citation_verified: bool | None = None

    def source_ref(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "pass": self.pass_label,
            "source_file_id": self.source_file_id,
            "page": self.source_page,
            "section": self.source_section,
            "quote": self.supporting_quote,
            "snapshot_id": self.source_snapshot_id,
            "confidence": self.confidence,
            "citation_verified": self.citation_verified,
        }


@dataclass
class CanonicalRequirement:
    """One reconciled requirement before (or mapped from) persistence."""

    key: str
    requirement_text: str
    requirement_type: str
    mandatory: bool | None
    severity: str | None
    response_required: bool | None
    source_file_id: int | None
    source_page: int | None
    source_section: str | None
    source_quote: str | None
    source_snapshot_id: int | None
    confidence: float | None
    found_by: list[str]
    candidates: list[Candidate]
    independently_confirmed: bool = False
    flags: list[str] = field(default_factory=list)
    disagreements: dict[str, Any] = field(default_factory=dict)
    key_values: dict[str, Any] = field(default_factory=dict)
    clause_refs: list[str] = field(default_factory=list)
    possible_duplicate_of: list[str] = field(default_factory=list)
    status: str = "unreviewed"
    status_reason: str | None = None
    requirement_id: int | None = None
    superseded_by_key: str | None = None
    stale: bool = False

    @property
    def source_refs(self) -> list[dict[str, Any]]:
        return [c.source_ref() for c in self.candidates]

    @property
    def has_source_location(self) -> bool:
        return self.source_file_id is not None and bool(self.source_quote)

    @property
    def is_critical(self) -> bool:
        return self.severity == "critical"

    @property
    def potentially_mandatory(self) -> bool:
        return self.mandatory is not False

    def reconciliation_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "found_by": self.found_by,
            "flags": self.flags,
            "disagreements": self.disagreements,
            "possible_duplicate_of": self.possible_duplicate_of,
            "candidate_ids": [c.candidate_id for c in self.candidates],
        }


@dataclass(frozen=True)
class ValidatorResult:
    """Deterministic validator output contract (§15.7)."""

    validator: str
    status: str
    reason: str
    evidence: dict[str, Any]
    validator_version: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "validator": self.validator,
            "status": self.status,
            "reason": self.reason,
            "evidence": self.evidence,
            "validator_version": self.validator_version,
        }


def max_severity(values: list[str | None]) -> str | None:
    known = [v for v in values if v in SEVERITY_ORDER]
    if not known:
        return None
    return max(known, key=lambda v: SEVERITY_ORDER[v])
