"""Decision provider protocol.

JEV, rule fallback, and LLM fallback implementations belong to later phases.
Phase 0 only fixes the interface those providers must satisfy.
"""

from __future__ import annotations

from typing import Protocol


class DecisionProvider(Protocol):
    name: str

    def decide(self, *, bundle_name: str, bundle_version: str, state: dict) -> dict:
        """Return a recommendation payload. Must not perform a consequential action."""
