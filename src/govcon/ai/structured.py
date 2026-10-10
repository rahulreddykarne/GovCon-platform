"""Run one registry-managed prompt and persist a reproducible ``ai_analyses`` row.

Pipeline (§37.4): prompt registry → system prompt (shared fragments + task
prompt) → bounded user context (required variables) → provider call through
the AI gateway → JSON parse → Pydantic schema → persist with prompt name,
version, hash, schema version, settings, input hash, and context manifest.

Malformed output fails closed with ``StructuredCallError``; nothing is stored
as a valid analysis.

Data-classification policy is enforced here, before any provider is resolved:

1. ``classification`` is a required argument; callers state what they send.
2. The prompt's ``allowed_data_classes`` front matter must list it.
3. The prompt's ``allowed_providers`` front matter (when set) must list the
   resolved provider.
4. The AI gateway (``AI_EXTERNAL_ALLOWED_FOR_*``) must allow an external call.

A refusal raises ``StructuredCallError("blocked_by_policy", ...)``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from govcon.ai.budget import AIBudgetExceeded, complete_with_budget
from govcon.ai.gateway import AIGatewayBlocked, authorize_external_call
from govcon.ai.providers import NoProviderConfigured, get_provider
from govcon.ai.providers.deepseek import parse_json_response
from govcon.ai.quality import assess_output_quality
from govcon.ai.schemas import SCHEMA_REGISTRY
from govcon.config import Settings, get_settings
from govcon.diagnostics import diagnostic_event, trace_phase
from govcon.models import AIAnalysis
from govcon.prompting.loader import PromptAsset
from govcon.prompting.registry import (
    PromptRegistryAbsent,
    load_prompt,
    load_prompt_from_disk,
)
from govcon.prompting.renderer import (
    PromptRenderError,
    render_system_prompt,
    render_user_context,
)
from govcon.security.classification import (
    DataClassification,
    opportunity_classification,
)

logger = logging.getLogger("govcon.ai.structured")

# Provider stop reasons meaning "ran out of output tokens" (OpenAI/DeepSeek, Anthropic).
_TRUNCATED = frozenset({"length", "max_tokens"})


class StructuredCallError(RuntimeError):
    """A structured AI step did not produce valid, schema-conformant output."""

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


@dataclass(frozen=True)
class StructuredCallResult:
    output: BaseModel
    # Even offline calls build an analysis; only its persistence/ID is deferred.
    analysis: AIAnalysis
    prompt: PromptAsset


_Output = TypeVar("_Output", bound=BaseModel)


def checked_output(output: BaseModel, schema: type[_Output]) -> _Output:
    """Require the schema a consumer expects, including registry misconfiguration."""
    if not isinstance(output, schema):
        raise StructuredCallError("schema_error", f"expected {schema.__name__}, received {type(output).__name__}")
    return output


@trace_phase("ai.structured.resolve_prompt")
def resolve_prompt(session: Session | None, prompt_name: str, settings: Settings) -> PromptAsset:
    """Registry is authoritative; disk bootstrap is explicitly opt-in."""
    prompt_root = settings.resolved_prompt_root()
    if session is not None:
        try:
            return load_prompt(session, prompt_name, version="active", prompt_root=prompt_root)
        except PromptRegistryAbsent as exc:
            if not settings.prompt_allow_disk_fallback:
                raise StructuredCallError("registry_absent", str(exc)) from exc
        except Exception as exc:
            raise StructuredCallError("registry_error", f"{type(exc).__name__}: {exc}") from exc
    elif not settings.prompt_allow_disk_fallback:
        raise StructuredCallError("registry_unavailable", "a registry session is required")
    try:
        return load_prompt_from_disk(prompt_root, prompt_name)
    except (ValueError, FileNotFoundError) as exc:
        raise StructuredCallError("prompt_unavailable", str(exc)) from exc


def _declared_list(prompt: PromptAsset, key: str) -> list[str]:
    raw = prompt.metadata.get(key) or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


def prompt_allowed_data_classes(prompt: PromptAsset) -> frozenset[DataClassification]:
    """Classifications the prompt declares in ``allowed_data_classes`` (PUBLIC when absent)."""
    declared = _declared_list(prompt, "allowed_data_classes")
    if not declared:
        return frozenset({DataClassification.PUBLIC})
    allowed: set[DataClassification] = set()
    for item in declared:
        try:
            allowed.add(DataClassification(item.upper()))
        except ValueError as exc:
            raise StructuredCallError("blocked_by_policy", f"prompt {prompt.name} declares unknown data class {item!r}") from exc
    return frozenset(allowed)


def enforce_prompt_policy(prompt: PromptAsset, *, classification: DataClassification, provider_name: str) -> None:
    """Refuse a call the prompt's front matter does not allow."""
    if classification is DataClassification.SECRET_CREDENTIAL:
        raise StructuredCallError("blocked_by_policy", "credentials are never sent to an AI provider")
    allowed = prompt_allowed_data_classes(prompt)
    if classification not in allowed:
        raise StructuredCallError(
            "blocked_by_policy",
            f"prompt {prompt.name} does not allow {classification.value} data "
            f"(allowed: {', '.join(sorted(c.value for c in allowed))})",
        )
    providers = [p.lower() for p in _declared_list(prompt, "allowed_providers")]
    if providers and provider_name not in providers:
        raise StructuredCallError(
            "blocked_by_policy",
            f"prompt {prompt.name} does not allow provider {provider_name!r} (allowed: {', '.join(providers)})",
        )


@dataclass(frozen=True)
class PreparedCall:
    """Everything a provider call needs, resolved inside a transaction.

    Built by :func:`prepare_structured_call`; executing it needs no session.
    """

    prompt: PromptAsset
    schema_cls: type[BaseModel]
    schema_version: str
    classification: DataClassification
    provider: Any
    provider_name: str | None
    model: str | None
    system_prompt: str
    user_prompt: str
    generation_settings: dict[str, Any]
    opportunity_id: int | None
    analysis_type: str
    variables: dict[str, Any]
    context_manifest: dict[str, Any]


@dataclass(frozen=True)
class ExecutedCall:
    """A validated provider response, not yet persisted."""

    output: BaseModel
    result: Any
    reservation: Any
    quality: str = "accepted"
    quality_reason: str = ""


@trace_phase("ai.structured.prepare_structured_call")
def prepare_structured_call(
    session: Session | None,
    *,
    opportunity_id: int | None,
    prompt_name: str,
    analysis_type: str,
    variables: dict[str, Any],
    context_manifest: dict[str, Any],
    settings: Settings | None = None,
    provider_name: str | None = None,
    model: str | None = None,
    classification: DataClassification,
) -> PreparedCall:
    """Resolve the prompt and enforce policy; refuse before any content is sent."""
    settings = settings or get_settings()
    from govcon.ai.routing import analysis_selection

    provider_name, model, route_fallback = analysis_selection(
        session, settings, provider_name=provider_name, model=model,
    )
    if not isinstance(classification, DataClassification):
        raise TypeError("classification must be a DataClassification")
    if session is not None and opportunity_id is not None:
        classification = opportunity_classification(session, opportunity_id, classification)
    prompt = resolve_prompt(session, prompt_name, settings)
    schema_version = prompt.metadata.get("schema_version", "")
    schema_cls = SCHEMA_REGISTRY.get(schema_version)
    if schema_cls is None:
        raise StructuredCallError("schema_error", f"prompt {prompt_name} declares unknown schema {schema_version!r}")

    # Policy first: a disallowed call is refused before any content is assembled.
    resolved_provider = (provider_name or settings.ai_primary_provider or "").strip().lower()
    enforce_prompt_policy(prompt, classification=classification, provider_name=resolved_provider)
    try:
        authorize_external_call(
            classification=classification,
            provider=resolved_provider,
            model=model or "default",
            purpose=prompt_name,
            settings=settings,
        )
    except AIGatewayBlocked as exc:
        raise StructuredCallError("blocked_by_policy", str(exc)) from exc

    try:
        system_prompt = render_system_prompt(prompt, settings.resolved_prompt_root())
        user_prompt = render_user_context(prompt, variables)
    except PromptRenderError as exc:
        raise StructuredCallError("render_error", str(exc)) from exc

    try:
        provider = get_provider(settings, provider_name=provider_name)
    except NoProviderConfigured as exc:
        raise StructuredCallError("no_provider", str(exc)) from exc

    generation_settings: dict[str, Any] = {
        "temperature": 0.0,
        "json_mode": True,
        "classification": classification.value,
        "max_tokens": settings.ai_max_output_tokens_per_call,
    }
    if model:
        generation_settings["model"] = model
    if route_fallback:
        generation_settings["route_fallback"] = route_fallback
    diagnostic_event("ai.call_prepared", provider=resolved_provider, model=model or "default",
                     prompt=prompt.name, prompt_version=prompt.version, schema=schema_version,
                     classification=classification.value,
                     bytes=len(system_prompt.encode("utf-8")) + len(user_prompt.encode("utf-8")))
    return PreparedCall(
        prompt=prompt,
        schema_cls=schema_cls,
        schema_version=schema_version,
        classification=classification,
        provider=provider,
        provider_name=provider_name,
        model=model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        generation_settings=generation_settings,
        opportunity_id=opportunity_id,
        analysis_type=analysis_type,
        variables=variables,
        context_manifest=context_manifest,
    )


@trace_phase("ai.structured.execute_prepared_call")
def execute_prepared_call(
    prepared: PreparedCall,
    *,
    settings: Settings | None = None,
    session: Session | None = None,
    engine: Any = None,
) -> ExecutedCall:
    """Call the provider and validate its JSON, retrying invalid output.

    Workers pass ``engine`` and no session, so no business transaction is open
    during the call; budget accounting commits on its own connection.
    """
    settings = settings or get_settings()
    attempts = 1 + max(0, int(settings.prompt_max_retries_on_invalid_json or 0))
    last_error: StructuredCallError | None = None
    for attempt in range(attempts):
        diagnostic_event("ai.validation_attempt", attempt=attempt + 1, attempts=attempts,
                         provider=prepared.provider_name, model=prepared.model,
                         prompt=prepared.prompt.name, schema=prepared.schema_version)
        try:
            result, reservation = complete_with_budget(prepared.provider, session,
                opportunity_id=prepared.opportunity_id,
                settings=settings,
                engine=engine,
                system_prompt=prepared.system_prompt,
                user_prompt=prepared.user_prompt,
                model=prepared.model,
                temperature=0.0,
                json_mode=True,
                classification=prepared.classification,
                purpose=prepared.prompt.name,
            )
        except AIBudgetExceeded as exc:
            raise StructuredCallError("budget_exceeded", str(exc)) from exc
        except AIGatewayBlocked as exc:
            raise StructuredCallError("blocked_by_policy", str(exc)) from exc
        except Exception as exc:
            raise StructuredCallError("provider_error", type(exc).__name__) from exc
        try:
            data = parse_json_response(result)
            validated = prepared.schema_cls.model_validate(data)
        except (json.JSONDecodeError, ValueError, ValidationError) as exc:
            diagnostic_event("ai.output_rejected", level=logging.WARNING, error_type=type(exc).__name__,
                             attempt=attempt + 1, schema=prepared.schema_version,
                             reason="truncated" if getattr(result, "finish_reason", None) in _TRUNCATED else "invalid_output")
            if isinstance(exc, ValidationError):
                diagnostic_event("ai.schema_errors", warnings=exc.error_count(), schema=prepared.schema_version)
                for issue in exc.errors(include_url=False, include_context=False, include_input=False)[:10]:
                    # Only declared top-level schema fields; arbitrary dict keys and
                    # validation messages may contain provider/document contents.
                    location = issue["loc"]
                    field = location[0] if location and location[0] in prepared.schema_cls.model_fields else "nested_or_root"
                    diagnostic_event("ai.schema_field_rejected", schema=prepared.schema_version,
                                     stage=str(field), reason=issue["type"])
            # Never log the provider response body; it can echo sensitive input.
            logger.warning("structured output rejected prompt=%s attempt=%d: %s", prepared.prompt.name, attempt + 1, type(exc).__name__)
            if getattr(result, "finish_reason", None) in _TRUNCATED:
                # The same request stops at the same cap; another attempt only spends tokens.
                raise StructuredCallError(
                    "output_truncated",
                    f"the provider stopped at the {settings.ai_max_output_tokens_per_call}-token output limit "
                    "before finishing (a reasoning model's thinking counts against it); raise "
                    "AI_MAX_OUTPUT_TOKENS_PER_CALL and retry",
                ) from exc
            last_error = StructuredCallError("invalid_output", f"attempt {attempt + 1}: {type(exc).__name__}")
            continue
        quality, reason = _output_quality(prepared.schema_cls, validated)
        diagnostic_event("ai.output_validated", quality=quality, schema=prepared.schema_version,
                         attempt=attempt + 1, latency_ms=getattr(result, "latency_ms", None))
        executed = ExecutedCall(
            output=validated, result=result, reservation=reservation, quality=quality, quality_reason=reason,
        )
        if quality == "incomplete":
            # Stored for review. A second call in the same request would spend the budget twice
            # on an answer that is already known to be thin. The next analysis run retries it.
            logger.warning("structured output sparse prompt=%s attempt=%d", prepared.prompt.name, attempt + 1)
            return executed
        return executed

    # A later invalid reply does not turn an earlier sparse reply into the answer.
    assert last_error is not None
    raise last_error


def _output_quality(schema_cls: type[BaseModel], output: BaseModel) -> tuple[str, str]:
    """Sparse solicitation analysis is incomplete. Other schemas may be legitimately empty."""
    from govcon.ai.schemas import SolicitationAnalysisV1

    if schema_cls is SolicitationAnalysisV1:
        return assess_output_quality(output)
    return "accepted", ""


def build_analysis(prepared: PreparedCall, executed: ExecutedCall) -> AIAnalysis:
    """The ``ai_analyses`` row for a validated call (not yet added to a session)."""
    manifest = dict(prepared.context_manifest)
    manifest.setdefault("opportunity_id", prepared.opportunity_id)
    input_hash = hashlib.sha256(
        json.dumps({"variables": prepared.variables, "manifest": manifest}, sort_keys=True, default=str).encode()
    ).hexdigest()
    result = executed.result
    reservation = executed.reservation
    return AIAnalysis(
        opportunity_id=prepared.opportunity_id,
        analysis_type=prepared.analysis_type,
        provider=getattr(result, "provider", None) or getattr(prepared.provider, "name", None),
        model=getattr(result, "model", None) or prepared.model,
        prompt_name=prepared.prompt.name,
        prompt_version=prepared.prompt.version,
        prompt_hash=prepared.prompt.content_hash,
        schema_version=prepared.schema_version,
        generation_settings={
            **prepared.generation_settings,
            "quality": executed.quality,
            "quality_reason": executed.quality_reason,
        },
        input_snapshot_hash=input_hash,
        context_manifest=manifest,
        output_json=executed.output.model_dump(mode="json"),
        source_refs=None,
        token_usage=getattr(result, "usage", None) or None,
        estimated_cost=reservation.cost if reservation is not None else None,
        latency_ms=getattr(result, "latency_ms", None),
    )


@trace_phase("ai.structured.persist_structured_result")
def persist_structured_result(
    session: Session | None, prepared: PreparedCall, executed: ExecutedCall
) -> StructuredCallResult:
    """Record the validated call as an ``ai_analyses`` row in the caller's transaction."""
    analysis = build_analysis(prepared, executed)
    if session is not None:
        session.add(analysis)
        session.flush()
    return StructuredCallResult(output=executed.output, analysis=analysis, prompt=prepared.prompt)


def run_structured_prompt(
    session: Session | None,
    *,
    opportunity_id: int | None,
    prompt_name: str,
    analysis_type: str,
    variables: dict[str, Any],
    context_manifest: dict[str, Any],
    settings: Settings | None = None,
    provider_name: str | None = None,
    model: str | None = None,
    classification: DataClassification,
) -> StructuredCallResult:
    """Prepare, call and persist in the caller's session (synchronous callers)."""
    settings = settings or get_settings()
    prepared = prepare_structured_call(
        session,
        opportunity_id=opportunity_id,
        prompt_name=prompt_name,
        analysis_type=analysis_type,
        variables=variables,
        context_manifest=context_manifest,
        settings=settings,
        provider_name=provider_name,
        model=model,
        classification=classification,
    )
    executed = execute_prepared_call(prepared, settings=settings, session=session)
    return persist_structured_result(session, prepared, executed)
