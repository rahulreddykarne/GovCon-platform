"""JEV structured decision provider."""

from __future__ import annotations

import json
import math
import time
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from govcon.ai.budget import AIBudgetExceeded, _engine_for_accounting, reserve
from govcon.ai.gateway import AIGatewayBlocked, authorize_external_call
from govcon.config import Settings, get_settings
from govcon.decision.bundles import bundle_definition
from govcon.decision.provider import (
    DecisionProviderInvalidResponse,
    DecisionProviderUnavailable,
    ProviderDecision,
)
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
        session=None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise DecisionProviderUnavailable("JEV API key is not configured")
        self._api_key = api_key.strip()
        self._base_url = (base_url or DEFAULT_JEV_BASE_URL).rstrip("/")
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._settings = settings or get_settings()
        self._session = session

    @classmethod
    def from_settings(cls, settings: Settings | None = None, *, session=None) -> JevDecisionProvider:
        settings = settings or get_settings()
        if not settings.jev_enabled:
            raise DecisionProviderUnavailable("JEV is disabled by JEV_ENABLED=false")
        if not settings.jev_api_key:
            raise DecisionProviderUnavailable("JEV_API_KEY is required for the JEV provider")
        return cls(
            api_key=settings.jev_api_key,
            base_url=settings.jev_base_url,
            settings=settings,
            session=session,
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
        try:
            authorize_external_call(
                classification=DataClassification(state.get("data_classification", "PROPRIETARY")),
                provider=self.name,
                model=self._model,
                purpose=f"decision_bundle:{bundle_name}",
                settings=self._settings,
            )
        except AIGatewayBlocked as exc:
            # Policy refusal is an unavailable provider: the engine falls back to rules.
            raise DecisionProviderUnavailable(f"JEV call blocked by data policy: {exc}") from exc
        body = {
            "model": self._model,
            "state": state,
            "questions": questions,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        from govcon.ai.replay import active_recorder

        budget_opportunity_id = state.get("budget_opportunity_id")
        recorder = active_recorder()
        if recorder is not None:
            # A recorded run: the request is sent later with no transaction open.
            engine = _engine_for_accounting(self._session.get_bind()) if self._session is not None else None
            response, latency_ms = recorder.call(
                ("jev", endpoint, self._model, body),
                lambda: self._post(endpoint, body, headers, bundle_name, budget_opportunity_id,
                                   session=None, engine=engine),
            )
        else:
            response, latency_ms = self._post(endpoint, body, headers, bundle_name, budget_opportunity_id,
                                              session=self._session)
        if response.status_code >= 400:
            # The body can echo the submitted state; keep only the status.
            raise DecisionProviderUnavailable(f"JEV responded with HTTP {response.status_code}")

        # Everything below validates untrusted output: any malformed part is a
        # typed provider error, so the engine falls back to the rules provider.
        try:
            data = response.json()
        except ValueError as exc:
            raise DecisionProviderInvalidResponse("JEV response was not valid JSON") from exc
        if not isinstance(data, dict):
            raise DecisionProviderInvalidResponse(f"JEV response was a JSON {type(data).__name__}, not an object")
        answers = data.get("answers") or data.get("result") or data.get("decisions")
        if not isinstance(answers, dict):
            raise DecisionProviderUnavailable("JEV response did not include an answers map")
        model = data.get("model", self._model)
        if model is not None and not isinstance(model, str):
            raise DecisionProviderInvalidResponse("JEV response model is not a string")
        normalized = _normalize_answers(questions, answers)
        confidence = _aggregate_confidence(answers)
        raw_usage = data.get("usage")
        usage = raw_usage if isinstance(raw_usage, dict) else {}
        cost = _parse_cost(usage.get("cost_usd"))
        return ProviderDecision(
            provider=self.name,
            model=model,
            result=normalized,
            confidence=confidence,
            cost=cost,
            latency_ms=latency_ms,
            raw_response={"usage": usage},
        )

    def _post(self, endpoint: str, body: dict[str, Any], headers: dict[str, str], bundle_name: str,
              budget_opportunity_id: int | None, *, session: Session | None = None,
              engine: Engine | None = None) -> tuple[httpx.Response, int]:
        """Reserve budget and send the request; returns the response and its latency."""
        try:
            reservation = reserve(session, opportunity_id=budget_opportunity_id,
                settings=self._settings, system_prompt="", user_prompt=json.dumps(body, default=str),
                purpose=f"decision_bundle:{bundle_name}", provider=self.name, model=self._model, engine=engine)
        except AIBudgetExceeded as exc:
            raise DecisionProviderUnavailable(str(exc)) from exc
        started = time.monotonic()
        try:
            with httpx.Client(timeout=self._timeout_seconds) as client:
                response = client.post(endpoint, json=body, headers=headers)
        except httpx.HTTPError as exc:  # pragma: no cover - network-dependent
            if reservation is not None:
                reservation.finish()
            raise DecisionProviderUnavailable(f"JEV request failed: {exc}") from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        # JEV has no guaranteed token/output contract: retain the conservative
        # reservation even on a successful response rather than crediting it.
        if reservation is not None:
            reservation.finish()
        return response, latency_ms

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
            if confidence is None:
                continue
            if isinstance(confidence, bool) or not isinstance(confidence, (float, int)):
                raise DecisionProviderInvalidResponse("JEV answer confidence is not a number")
            if not math.isfinite(confidence) or not 0.0 <= float(confidence) <= 1.0:
                raise DecisionProviderInvalidResponse("JEV answer confidence is not a finite value in [0, 1]")
            values.append(float(confidence))
    if not values:
        return None
    return sum(values) / len(values)


def _parse_cost(raw: Any) -> Decimal | None:
    """``usage.cost_usd`` as a finite, non-negative Decimal; absent is None."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if isinstance(raw, bool) or not isinstance(raw, (float, int, str)):
        raise DecisionProviderInvalidResponse("JEV usage.cost_usd is not a number")
    try:
        cost = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError) as exc:
        raise DecisionProviderInvalidResponse("JEV usage.cost_usd is not a number") from exc
    if not cost.is_finite() or cost < 0:
        raise DecisionProviderInvalidResponse("JEV usage.cost_usd is not a finite, non-negative amount")
    return cost
