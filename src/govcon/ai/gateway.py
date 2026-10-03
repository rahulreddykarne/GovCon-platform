"""Classify content and block disallowed external model calls."""

from __future__ import annotations

import logging

from govcon.config import Settings, get_settings
from govcon.security.classification import DataClassification

logger = logging.getLogger("govcon.ai.gateway")


class AIGatewayBlocked(Exception):
    """The classification policy refused an external model call."""

    def __init__(self, classification: DataClassification) -> None:
        self.classification = classification
        super().__init__(f"external model call blocked for {classification.value}")


def external_call_allowed(classification: DataClassification, settings: Settings | None = None) -> bool:
    """Return whether an external provider may receive this classification.

    ``SECRET_CREDENTIAL`` and ``UNKNOWN`` are always blocked. FCI, CUI, and proprietary data
    follow configuration and are blocked by default.
    """
    settings = settings or get_settings()
    if classification is DataClassification.SECRET_CREDENTIAL:
        return False
    if classification is DataClassification.PUBLIC:
        return True
    if classification is DataClassification.PROPRIETARY:
        return settings.ai_external_allowed_for_proprietary
    if classification is DataClassification.FCI:
        return settings.ai_external_allowed_for_fci
    if classification is DataClassification.CUI:
        return settings.ai_external_allowed_for_cui
    return False


def authorize_external_call(
    *,
    classification: DataClassification,
    provider: str,
    model: str,
    purpose: str,
    settings: Settings | None = None,
) -> None:
    """Log the decision and raise when the call is not allowed.

    The log line records provider, model, classification, and purpose.
    It does not accept secret material as a separate field.
    """
    settings = settings or get_settings()
    allowed = external_call_allowed(classification, settings)
    logger.info(
        "ai_gateway decision=%s classification=%s provider=%s model=%s purpose=%s",
        "allow" if allowed else "block",
        classification.value,
        provider,
        model,
        purpose,
    )
    if not allowed:
        raise AIGatewayBlocked(classification)
