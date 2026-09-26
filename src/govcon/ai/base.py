"""Provider protocol. Phase 0 does not call a model."""

from __future__ import annotations

from typing import Protocol


class AIProvider(Protocol):
    name: str

    def complete(self, *, prompt: str, model: str, settings: dict) -> str:
        """Return model text. Implementations arrive with the provider phases."""
