"""Generative fallback provider for decision refinement."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from govcon.ai.providers import NoProviderConfigured, get_provider
from govcon.ai.providers.deepseek import parse_json_response
from govcon.config import Settings, get_settings
from govcon.decision.provider import DecisionProviderUnavailable, ProviderDecision
from govcon.security.classification import DataClassification


class LLMDecisionProvider:
    """Optional fallback that asks a generative model for structured hints.

    This provider is intentionally conservative: it returns a partial JSON patch
    that the engine merges with deterministic rule output.
    """

    name = "llm"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    def decide(
        self,
        *,
        bundle_name: str,
        bundle_version: str,
        state: dict[str, Any],
    ) -> ProviderDecision:
        try:
            provider = get_provider(self._settings)
        except NoProviderConfigured as exc:
            raise DecisionProviderUnavailable(str(exc)) from exc

        system_prompt = (
            "You are a structured decision fallback. "
            "Return only JSON object fields that should be updated for the requested bundle. "
            "Do not claim facts not present in the provided state."
        )
        user_prompt = json.dumps(
            {
                "bundle_name": bundle_name,
                "bundle_version": bundle_version,
                "state": state,
                "required_behavior": [
                    "Prefer review when uncertain.",
                    "Do not approve consequential actions.",
                    "Keep outputs concise and schema-compatible.",
                ],
            },
            sort_keys=True,
            default=str,
        )
        try:
            result = provider.complete(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.0,
                json_mode=True,
                classification=DataClassification.PROPRIETARY,
                purpose="decision_llm_fallback",
            )
        except Exception as exc:  # pragma: no cover - network/provider dependent
            raise DecisionProviderUnavailable(str(exc)) from exc

        try:
            parsed = parse_json_response(result)
        except Exception:
            try:
                parsed = json.loads(getattr(result, "content", "") or "{}")
            except Exception as exc:
                raise DecisionProviderUnavailable(f"invalid LLM fallback JSON: {exc}") from exc

        if not isinstance(parsed, dict):
            raise DecisionProviderUnavailable("LLM fallback did not return a JSON object")

        confidence = parsed.pop("confidence", None)
        if isinstance(confidence, str):
            confidence = {"low": 0.35, "medium": 0.62, "high": 0.85}.get(confidence.lower())
        if confidence is not None and not isinstance(confidence, (float, int)):
            confidence = None

        usage = getattr(result, "usage", None) or {}
        cost = usage.get("cost_usd")
        decimal_cost = Decimal(str(cost)) if isinstance(cost, (float, int, str)) and str(cost).strip() else None

        return ProviderDecision(
            provider=self.name,
            model=getattr(result, "model", None),
            result=parsed,
            confidence=float(confidence) if isinstance(confidence, (float, int)) else None,
            cost=decimal_cost,
            latency_ms=getattr(result, "latency_ms", None),
            raw_response={"usage": usage},
        )
