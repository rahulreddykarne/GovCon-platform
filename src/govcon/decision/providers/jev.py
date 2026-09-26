"""JEV structured decision provider."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

import httpx

from govcon.ai.gateway import authorize_external_call
from govcon.config import Settings, get_settings
from govcon.decision.bundles import bundle_definition
from govcon.decision.provider import DecisionProviderUnavailable, ProviderDecision
from govcon.security.classification import DataClassification

DEFAULT_JEV_BASE_URL = "https://api.typesafe.ai"
DEFAULT_JEV_MODEL = "jev-latest"


class JevDecisionProvider:
    """Calls a JEV-compatible decision endpoint.

    Verified 2026-09-26 (unauthenticated contract checks):
    - POST /v1/systemone expects model/state/questions shape.
    - Bearer auth required.
    - /v1/chat/completions is not a valid JEV endpoint.
    """

    name = "jev"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        model: str = DEFAULT_JEV_MODEL,
        timeout_seconds: float = 30.0,
        settings: Settings | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise DecisionProviderUnavailable("JEV API key is not configured")
        self._api_key = api_key.strip()
        self._base_url = (base_url or DEFAULT_JEV_BASE_URL).rstrip("/")
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._settings = settings or get_settings()

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "JevDecisionProvider":
        settings = settings or get_settings()
        if not settings.jev_enabled:
            raise DecisionProviderUnavailable("JEV is disabled by JEV_ENABLED=false")
        if not settings.jev_api_key:
            raise DecisionProviderUnavailable("JEV_API_KEY is required for the JEV provider")
        return cls(
            api_key=settings.jev_api_key,
            base_url=settings.jev_base_url,
            settings=settings,
        )

    def decide(
        self,
        *,
        bundle_name: str,
        bundle_version: str,
        state: dict[str, Any],
    ) -> ProviderDecision:
        definition = bundle_definition(bundle_name)
        questions = definition.jev_questions
        endpoint = self._resolve_endpoint()
        authorize_external_call(
            classification=DataClassification.PROPRIETARY,
            provider=self.name,
            model=self._model,
            purpose=f"decision_bundle:{bundle_name}",
            settings=self._settings,
        )
        body = {
            "model": self._model,
            "state": state,
            "questions": questions,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        started = time.monotonic()
        try:
            with httpx.Client(timeout=self._timeout_seconds) as client:
                response = client.post(endpoint, json=body, headers=headers)
        except httpx.HTTPError as exc:  # pragma: no cover - network-dependent
            raise DecisionProviderUnavailable(f"JEV request failed: {exc}") from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        if response.status_code >= 400:
            detail = (response.text or "").strip()
            raise DecisionProviderUnavailable(
                f"JEV responded with HTTP {response.status_code}: {detail[:300]}"
            )

        data = response.json()
        answers = data.get("answers") or data.get("result") or data.get("decisions")
        if not isinstance(answers, dict):
            raise DecisionProviderUnavailable("JEV response did not include an answers map")
        normalized = _normalize_answers(questions, answers)
        confidence = _aggregate_confidence(answers)
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        raw_cost = usage.get("cost_usd")
        cost = None
        if isinstance(raw_cost, (float, int, str)) and str(raw_cost).strip():
            cost = Decimal(str(raw_cost))
        return ProviderDecision(
            provider=self.name,
            model=data.get("model", self._model),
            result=normalized,
            confidence=confidence,
            cost=cost,
            latency_ms=latency_ms,
            raw_response={"usage": usage},
        )

    def _resolve_endpoint(self) -> str:
        if self._base_url.endswith("/v1/systemone") or self._base_url.endswith("/api/v1/decisions"):
            return self._base_url
        if "/v1/" in self._base_url:
            return self._base_url
        return f"{self._base_url}/v1/systemone"


def _normalize_answers(
    questions: dict[str, dict[str, Any]],
    answers: dict[str, Any],
) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, question in questions.items():
        answer = answers.get(key)
        if isinstance(answer, dict):
            qtype = question.get("type")
            if qtype == "choice":
                value = answer.get("choice", answer.get("value"))
            elif qtype == "noul":
                raw = answer.get("noul", answer.get("value"))
                if isinstance(raw, bool):
                    value = raw
                elif isinstance(raw, (float, int)):
                    value = raw >= 0.5
                else:
                    value = None
            elif qtype == "score":
                value = answer.get("score", answer.get("value"))
            else:
                value = answer.get("value")
            if value is not None:
                normalized[key] = value
        elif answer is not None:
            normalized[key] = answer
    return normalized


def _aggregate_confidence(answers: dict[str, Any]) -> float | None:
    values: list[float] = []
    for answer in answers.values():
        if isinstance(answer, dict):
            confidence = answer.get("confidence")
            if isinstance(confidence, (float, int)):
                values.append(float(confidence))
    if not values:
        return None
    return sum(values) / len(values)
