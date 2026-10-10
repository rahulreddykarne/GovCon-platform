"""JEV structured decision provider."""

from __future__ import annotations

import json
import math
import time
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Any

import httpx

from govcon.ai.budget import AIBudgetExceeded, reserve
from govcon.ai.gateway import AIGatewayBlocked, authorize_external_call
from govcon.ai.usage_log import record_call
from govcon.config import Settings, get_settings
from govcon.decision.bundles import bundle_definition
from govcon.decision.provider import (
    DecisionProviderInvalidResponse,
    DecisionProviderUnavailable,
    ProviderDecision,
)
from govcon.diagnostics import trace_phase
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
    def from_settings(cls, settings: Settings | None = None, *, session=None) -> "JevDecisionProvider":
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

    @trace_phase("decision.providers.jev.decide")
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
        classification = DataClassification(state.get("data_classification", "PROPRIETARY"))
        raw_opportunity_id = state.get("budget_opportunity_id")
        opportunity_id = raw_opportunity_id if isinstance(raw_opportunity_id, int) else None
        self._authorize(classification, bundle_name, opportunity_id, session=self._session)
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
            engine = None
            if self._session is not None:
                bind = self._session.get_bind()
                engine = getattr(bind, "engine", bind)
            response, latency_ms = recorder.call(
                ("jev", endpoint, self._model, body),
                lambda: self._post(endpoint, body, headers, bundle_name, budget_opportunity_id,
                                   session=None, engine=engine),
            )
        else:
            response, latency_ms = self._post(endpoint, body, headers, bundle_name, budget_opportunity_id,
                                              session=self._session)
        purpose = f"decision_bundle:{bundle_name}"
        if response.status_code >= 400:
            record_call(self._session, provider=self.name, purpose=purpose, status="failed", model=self._model,
                        latency_ms=latency_ms, opportunity_id=opportunity_id)
            # The body can echo the submitted state; keep only the status.
            raise DecisionProviderUnavailable(f"JEV responded with HTTP {response.status_code}")

        # Everything below validates untrusted output: any malformed part is a
        # typed provider error, so the engine falls back to the rules provider.
        try:
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
            usage = data.get("usage")
            if not isinstance(usage, dict):
                usage = {}
            cost = _parse_cost(usage.get("cost_usd"))
        except (DecisionProviderInvalidResponse, DecisionProviderUnavailable):
            record_call(self._session, provider=self.name, purpose=purpose, status="failed", model=self._model,
                        latency_ms=latency_ms, opportunity_id=opportunity_id)
            raise
        record_call(self._session, provider=self.name, purpose=purpose, status="succeeded",
                    model=model or self._model, usage=usage, latency_ms=latency_ms, opportunity_id=opportunity_id)
        return ProviderDecision(
            provider=self.name,
            model=model,
            result=normalized,
            confidence=confidence,
            cost=cost,
            latency_ms=latency_ms,
            raw_response={"usage": usage},
        )

    @trace_phase("decision.providers.jev.post")
    def _post(self, endpoint: str, body: dict[str, Any], headers: dict[str, str], bundle_name: str,
              budget_opportunity_id: int | None, *, session=None, engine=None) -> tuple[httpx.Response, int]:
        """Gate, reserve budget and send the request; returns the response and its latency."""
        opportunity_id = budget_opportunity_id if isinstance(budget_opportunity_id, int) else None
        # The body carries the state that is sent, so its classification is the one gated here.
        state = body.get("state") if isinstance(body.get("state"), dict) else {}
        classification = DataClassification(state.get("data_classification", "PROPRIETARY"))
        self._authorize(classification, bundle_name, opportunity_id, session=session, engine=engine)
        try:
            reservation = reserve(session, opportunity_id=budget_opportunity_id,
                settings=self._settings, system_prompt="", user_prompt=json.dumps(body, default=str),
                purpose=f"decision_bundle:{bundle_name}", provider=self.name, model=self._model, engine=engine)
        except AIBudgetExceeded as exc:
            raise DecisionProviderUnavailable(str(exc)) from exc
        started = time.monotonic()
        try:
            from govcon.http import build_client

            with build_client(self._settings, timeout=self._timeout_seconds) as client:
                response = client.post(endpoint, json=body, headers=headers)
        except httpx.HTTPError as exc:  # pragma: no cover - network-dependent
            if reservation is not None:
                reservation.finish()
            record_call(session, provider=self.name, purpose=f"decision_bundle:{bundle_name}", status="failed",
                        model=self._model, engine=engine, opportunity_id=opportunity_id)
            raise DecisionProviderUnavailable(f"JEV request failed: {exc}") from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        # JEV has no guaranteed token/output contract: retain the conservative
        # reservation even on a successful response rather than crediting it.
        # An answered call is recorded as succeeded (empty usage keeps the full
        # reservation); an HTTP error stays failed.
        if reservation is not None:
            answered = response.status_code < 400
            reservation.finish(SimpleNamespace(usage={}, model=self._model) if answered else None)
        return response, latency_ms

    def _authorize(self, classification: DataClassification, bundle_name: str, opportunity_id: int | None,
                   *, session=None, engine=None) -> None:
        purpose = f"decision_bundle:{bundle_name}"
        try:
            authorize_external_call(classification=classification, provider=self.name, model=self._model,
                                    purpose=purpose, settings=self._settings)
        except AIGatewayBlocked as exc:
            record_call(session, provider=self.name, purpose=purpose, status="blocked", model=self._model,
                        opportunity_id=opportunity_id, engine=engine)
            # Policy refusal is an unavailable provider: the engine falls back to rules.
            raise DecisionProviderUnavailable(f"JEV call blocked by data policy: {exc}") from exc

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
