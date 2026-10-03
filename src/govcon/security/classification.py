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


def opportunity_classification(session: Session, opportunity_id: int, declared: DataClassification) -> DataClassification:
    # Retained historical documents can still contribute to derived content.
    from govcon.models import StoredFile
    classes = session.scalars(select(StoredFile.classification).where(StoredFile.opportunity_id == opportunity_id)).all()
    classes += [row.classification for row in session.new.union(session.dirty)
                if isinstance(row, StoredFile) and row.opportunity_id == opportunity_id]
    return strictest_classification(declared, *classes)


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
