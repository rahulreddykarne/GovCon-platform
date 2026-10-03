"""Shared result and error types for AI providers.

Every provider exposes the same ``complete()`` keyword interface as
:class:`govcon.ai.providers.deepseek.DeepSeekProvider` and returns an object
with ``content``, ``model``, ``provider``, ``usage``, ``latency_ms`` and
``finish_reason``. Errors carry the HTTP status only: provider response bodies
can echo prompt content and are never logged or kept.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CompletionResult:
    content: str
    model: str
    provider: str
    usage: dict = field(default_factory=dict)
    latency_ms: int = 0
    finish_reason: str | None = None


class ProviderAPIError(RuntimeError):
    """The provider returned an error status, or could not be reached (status ``None``)."""

    def __init__(self, provider: str, status_code: int | None) -> None:
        self.provider = provider
        self.status_code = status_code
        detail = f"HTTP {status_code}" if status_code is not None else "a connection error"
        super().__init__(f"{provider} API returned {detail}")


class ProviderRefusal(RuntimeError):
    """The model declined the request; there is no usable content."""

    def __init__(self, provider: str, model: str, category: str | None = None) -> None:
        self.provider = provider
        self.model = model
        self.category = category
        super().__init__(f"{provider} model {model} declined the request" + (f" ({category})" if category else ""))
