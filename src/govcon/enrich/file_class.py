"""When a stored file may be marked PUBLIC.

A filename is not evidence. PUBLIC requires the bytes on disk to match the
recorded SHA-256, a source that is not the filename, and extracted text from
those bytes. Every change is an audit event. The event stores the class,
the hash, and the source. It does not store the document text.
"""

from __future__ import annotations

import hashlib

from sqlalchemy.orm import Session

from govcon.audit import record_audit
from govcon.models import StoredFile
from govcon.security.classification import DataClassification

_PDF_MAGIC = b"%PDF-"


def public_blockers(row: StoredFile, data: bytes | None) -> list[str]:
    """Reasons this row must stay off the public path. Empty means it may be PUBLIC."""
    reasons: list[str] = []
    if not data:
        reasons.append("stored bytes were not read")
    elif not row.sha256 or hashlib.sha256(data).hexdigest() != row.sha256:
        reasons.append("stored bytes do not match the recorded SHA-256")
    else:
        reasons.extend(_content_blockers(row, data))
    origin = (row.source_origin or "").strip()
    filename = (row.filename or "").strip()
    if not origin or origin == "legacy_unknown":
        reasons.append("no source provenance")
    elif filename and origin == filename:
        reasons.append("source provenance is only the filename")
    elif not (row.url or "").strip() and origin == "government_feed":
        reasons.append("government provenance has no URL")
    return reasons


def _content_blockers(row: StoredFile, data: bytes) -> list[str]:
    name = (row.filename or "").lower()
    mime = (row.mime_type or "").lower()
    if name.endswith(".pdf") or mime == "application/pdf":
        if not data.startswith(_PDF_MAGIC):
            return ["bytes are not a PDF"]
    if row.extraction_status != "success":
        return ["text was not extracted from these bytes"]
    if not (row.extracted_text or "").strip():
        return ["extracted text is empty"]
    return []


def apply_classification(
    session: Session,
    row: StoredFile,
    new: DataClassification,
    *,
    reason: str,
) -> bool:
    """Set ``new`` when it changes the row. Returns whether the class changed.

    PUBLIC is refused while ``public_blockers`` would apply. The caller passes
    the bytes check by only requesting PUBLIC after ``public_blockers`` is empty.
    A stricter class already on the row is kept.
    """
    if new is DataClassification.PUBLIC and row.classification not in {"UNKNOWN", "PUBLIC"}:
        return False
    if row.classification == new.value:
        return False
    previous = row.classification
    row.classification = new.value
    record_audit(
        session,
        action_type="file_classification_changed",
        opportunity_id=row.opportunity_id,
        entity_type="files",
        entity_id=row.id,
        old_value={"classification": previous, "sha256": row.sha256, "filename": row.filename},
        new_value={
            "classification": new.value,
            "reason": reason,
            "sha256": row.sha256,
            "source_origin": row.source_origin,
            "url": row.url,
        },
    )
    return True


def promote_public_if_verified(session: Session, row: StoredFile, data: bytes | None) -> list[str]:
    """Mark a verified government or local file PUBLIC. Returns the blockers otherwise."""
    blockers = public_blockers(row, data)
    if blockers:
        return blockers
    apply_classification(session, row, DataClassification.PUBLIC, reason="bytes_provenance_and_content_verified")
    return []
