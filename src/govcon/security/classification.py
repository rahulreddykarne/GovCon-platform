"""Data classification used by the AI gateway before any external model call."""

from __future__ import annotations

from enum import Enum


class DataClassification(str, Enum):
    PUBLIC = "PUBLIC"
    PROPRIETARY = "PROPRIETARY"
    FCI = "FCI"
    CUI = "CUI"
    SECRET_CREDENTIAL = "SECRET_CREDENTIAL"


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
