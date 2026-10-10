"""Data classification used by the AI gateway before any external model call."""

from __future__ import annotations

from enum import Enum

from sqlalchemy import select
from sqlalchemy.orm import Session


class DataClassification(str, Enum):
    PUBLIC = "PUBLIC"
    PROPRIETARY = "PROPRIETARY"
    FCI = "FCI"
    CUI = "CUI"
    UNKNOWN = "UNKNOWN"
    SECRET_CREDENTIAL = "SECRET_CREDENTIAL"


def strictest_classification(*values: DataClassification | str | None) -> DataClassification:
    """Unknown metadata fails closed. Never lower a caller's declared class."""
    order = list(DataClassification)
    classes = []
    for value in values:
        try:
            classes.append(DataClassification(value))
        except (ValueError, TypeError):
            classes.append(DataClassification.UNKNOWN)
    return max(classes, key=order.index, default=DataClassification.PUBLIC)


def has_sendable_content(row: object) -> bool:
    """True when this file contributed extracted text that a model call would send.

    A failed download or a stored row with no extracted text is not document
    content: its classification must not decide whether a call is allowed.
    """
    status = getattr(row, "extraction_status", None) or getattr(row, "text_extraction_status", None)
    if status == "download_failed":
        return False
    pages = getattr(row, "page_texts", None)
    if isinstance(pages, list) and any(isinstance(page, str) and page.strip() for page in pages):
        return True
    for attr in ("extracted_text", "text"):
        value = getattr(row, attr, None)
        if isinstance(value, str) and value.strip():
            return True
    return False


def payload_classification(*items: object) -> DataClassification:
    """Strictest class of files whose content is actually sent.

    Failed downloads and empty extractions are omitted. An empty set is PUBLIC
    (no content is leaving); a sendable UNKNOWN file still fails closed.
    """
    return strictest_classification(*(
        getattr(item, "classification", None) for item in items if has_sendable_content(item)
    ))


def opportunity_classification(session: Session, opportunity_id: int, declared: DataClassification) -> DataClassification:
    # Retained historical documents can still contribute to derived content.
    # A failed download has no bytes: its UNKNOWN class is not document content.
    # A stored file that is still UNKNOWN, or any controlled class, still counts.
    from govcon.models import StoredFile
    rows = list(session.scalars(select(StoredFile).where(StoredFile.opportunity_id == opportunity_id)).all())
    seen = {row.id for row in rows if row.id is not None}
    for row in session.new.union(session.dirty):
        if isinstance(row, StoredFile) and row.opportunity_id == opportunity_id and row.id not in seen:
            rows.append(row)
    classes = [row.classification for row in rows if _counts_toward_gateway(row)]
    return strictest_classification(declared, *classes)


def _counts_toward_gateway(row: object) -> bool:
    classification = getattr(row, "classification", None)
    if classification in {"PROPRIETARY", "FCI", "CUI", "SECRET_CREDENTIAL"}:
        return True
    return has_sendable_content(row)


# FCI and CUI are never inferred. Callers must pass them explicitly.
_DEFAULT_BY_KIND: dict[str, DataClassification] = {
    "solicitation": DataClassification.PUBLIC,
    "award": DataClassification.PUBLIC,
    "supplier_quote": DataClassification.PROPRIETARY,
    "internal_pricing": DataClassification.PROPRIETARY,
    "proposal_draft": DataClassification.PROPRIETARY,
    "company_strategy": DataClassification.PROPRIETARY,
    "portal_credential": DataClassification.SECRET_CREDENTIAL,
}


def classify(kind: str, *, explicit: DataClassification | None = None) -> DataClassification:
    """Return the classification for a content kind.

    ``explicit`` is required for FCI and CUI. Unknown kinds fail instead of
    defaulting to a sendable class.
    """
    if explicit is not None:
        return explicit
    try:
        return _DEFAULT_BY_KIND[kind]
    except KeyError as exc:
        raise ValueError(f"unknown content kind: {kind}") from exc
