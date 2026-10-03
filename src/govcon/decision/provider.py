"""Decision-provider interfaces for Phase 8."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol


class DecisionProviderError(RuntimeError):
    """Base provider error."""


class DecisionProviderUnavailable(DecisionProviderError):
    """Raised when a provider cannot process a request in this environment."""


class DecisionProviderInvalidResponse(DecisionProviderUnavailable):
    """The provider answered, but not with a usable decision (bad JSON, shape, or values).

    A subclass of ``DecisionProviderUnavailable`` so the engine falls back to
    the rules provider. The message never echoes the response body.
    """


@dataclass(frozen=True)
class ProviderDecision:
    """Structured provider output used by the decision engine."""

    provider: str
    model: str | None
    result: dict[str, Any]
    confidence: float | None = None
    cost: Decimal | None = None
    latency_ms: int | None = None
    raw_response: dict[str, Any] | None = None


class DecisionProvider(Protocol):
    """Contract implemented by all decision providers."""

    name: str

    def decide(
        self,
        *,
        bundle_name: str,
        bundle_version: str,
        state: dict[str, Any],
    ) -> ProviderDecision:
        """Return a structured decision signal for one bundle."""
