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

from govcon.ai.budget import (
    AIBudgetExceeded,
    complete_with_budget,
    mark_replaced_by_split,
    output_token_limit,
)
from govcon.ai.gateway import AIGatewayBlocked, authorize_external_call
from govcon.ai.providers import NoProviderConfigured, get_provider
from govcon.ai.providers.deepseek import parse_json_response
from govcon.ai.quality import assess_output_quality
from govcon.ai.schemas import SCHEMA_REGISTRY
from govcon.ai.spend_guard import SpendGuardExceeded, note_spend
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
    cached_analysis: AIAnalysis | None = None


@trace_phase("ai.structured.prepare_structured_call")
def _drop_unknown_root_keys(prepared: PreparedCall, data: Any) -> Any:
    """Drop top-level keys a strict schema does not declare, instead of rejecting the answer.

    JSON-mode models sometimes add an extra top-level key beside valid content;
    rejecting the whole answer cost a paid retry. The schema's own validators
    still reject an answer whose declared fields are empty.
    """
    schema_cls = prepared.schema_cls
    if schema_cls.model_config.get("extra") != "forbid" or not isinstance(data, dict):
        return data
    known = set(schema_cls.model_fields) | {field.alias for field in schema_cls.model_fields.values() if field.alias}
    unknown = [key for key in data if key not in known]
    if not unknown:
        return data
    # The count only: key names are model output.
    diagnostic_event("ai.unknown_keys_dropped", level=logging.INFO, schema=prepared.schema_version, dropped_keys=len(unknown))
    return {key: value for key, value in data.items() if key in known}


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

    def _record_blocked(reason: str) -> None:
        from govcon.ai.usage_log import record_call
        record_call(
            session, provider=resolved_provider or "none", purpose=prompt_name, status="blocked",
            model=model, opportunity_id=opportunity_id, finish_reason=reason,
        )

    try:
        enforce_prompt_policy(prompt, classification=classification, provider_name=resolved_provider)
    except StructuredCallError as exc:
        if exc.reason == "blocked_by_policy":
            _record_blocked(exc.detail)
        raise
    try:
        authorize_external_call(
            classification=classification,
            provider=resolved_provider,
            model=model or "default",
            purpose=prompt_name,
            settings=settings,
        )
    except AIGatewayBlocked as exc:
        _record_blocked(str(exc))
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
        "max_tokens": output_token_limit(settings, resolved_provider),
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
    cache_session = session
    opened: Session | None = None
    if cache_session is None and engine is not None:
        from sqlalchemy.orm import Session as OrmSession

        opened = OrmSession(engine)
        cache_session = opened
    try:
        cached = lookup_cached_analysis(cache_session, prepared)
    finally:
        if opened is not None:
            opened.close()
    if cached is not None:
        diagnostic_event("ai.part_cache_hit", prompt=prepared.prompt.name,
                         prompt_hash=prepared.prompt.content_hash, analysis_id=cached.id)
        return cached_execution(prepared, cached)
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
        except SpendGuardExceeded as exc:
            raise StructuredCallError("spend_guard", str(exc)) from exc
        except AIGatewayBlocked as exc:
            raise StructuredCallError("blocked_by_policy", str(exc)) from exc
        except Exception as exc:
            raise StructuredCallError("provider_error", type(exc).__name__) from exc
        try:
            usage = getattr(result, "usage", None) or {}
            tokens = None
            if isinstance(usage, dict):
                tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0) + int(
                    usage.get("completion_tokens") or usage.get("output_tokens") or 0
                )
            _charge_reservation(reservation, tokens=tokens or None)
        except SpendGuardExceeded as exc:
            raise StructuredCallError("spend_guard", str(exc)) from exc
        try:
            data = parse_json_response(result)
            if prepared.schema_version == "requirement_extraction.v1":
                from govcon.compliance.schemas import expand_compact_extraction

                data = expand_compact_extraction(data)
            data = _drop_unknown_root_keys(prepared, data)
            validated = prepared.schema_cls.model_validate(data)
        except (json.JSONDecodeError, ValueError, ValidationError) as exc:
            # Parse-error metadata only (message, position, sizes); never the response text.
            parse_detail = ({"parse_error": exc.msg, "position": exc.pos,
                             "characters": len(getattr(result, "content", "") or "")}
                            if isinstance(exc, json.JSONDecodeError) else {})
            diagnostic_event("ai.output_rejected", level=logging.WARNING, error_type=type(exc).__name__,
                             attempt=attempt + 1, schema=prepared.schema_version,
                             finish_reason=getattr(result, "finish_reason", None), **parse_detail,
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
                # The caller splits this part and retries the halves. Do not count this
                # parent reservation against the children.
                mark_replaced_by_split(reservation)
                raise StructuredCallError(
                    "output_truncated",
                    f"the provider stopped at the {output_token_limit(settings, prepared.provider_name)}-token "
                    "output limit before finishing (a reasoning model's thinking counts against it); "
                    "the part will be split and retried. If a split part still hits the cap, raise "
                    "AI_MAX_OUTPUT_TOKENS_PER_CALL",
                ) from exc
            call_id = getattr(result, "usage_call_id", None)
            if call_id is not None:
                from govcon.ai.usage_log import update_call_status
                update_call_status(
                    session, int(call_id), status="output_rejected",
                    finish_reason=getattr(result, "finish_reason", None), engine=engine,
                )
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


_VOLATILE_MANIFEST = frozenset({
    "part", "parts", "coverage", "omitted_sources", "warnings", "chunks",
})


def analysis_input_hash(prepared: PreparedCall) -> str:
    """Stable key: source variables + document sha. Part index is not hashed."""
    manifest = {
        key: value for key, value in dict(prepared.context_manifest).items()
        if key not in _VOLATILE_MANIFEST
    }
    manifest.setdefault("opportunity_id", prepared.opportunity_id)
    return hashlib.sha256(
        json.dumps({"variables": prepared.variables, "manifest": manifest}, sort_keys=True, default=str).encode()
    ).hexdigest()


def lookup_cached_analysis(session: Session | None, prepared: PreparedCall) -> AIAnalysis | None:
    """Reuse a complete analysis for the same prompt hash and document snapshot."""
    if session is None or prepared.opportunity_id is None:
        return None
    from sqlalchemy import select

    row = session.scalar(
        select(AIAnalysis)
        .where(
            AIAnalysis.opportunity_id == prepared.opportunity_id,
            AIAnalysis.prompt_name == prepared.prompt.name,
            AIAnalysis.prompt_hash == prepared.prompt.content_hash,
            AIAnalysis.input_snapshot_hash == analysis_input_hash(prepared),
        )
        .order_by(AIAnalysis.id.desc())
        .limit(1)
    )
    if row is None:
        return None
    quality = (row.generation_settings or {}).get("quality")
    if quality == "incomplete":
        return None
    return row


def cached_execution(prepared: PreparedCall, cached: AIAnalysis) -> ExecutedCall:
    """Rebuild an executed call from a stored successful part. No provider spend."""
    output = prepared.schema_cls.model_validate(cached.output_json)
    quality = (cached.generation_settings or {}).get("quality") or "accepted"
    reason = (cached.generation_settings or {}).get("quality_reason") or "cached"
    result = type("CachedResult", (), {
        "provider": cached.provider or "cache",
        "model": cached.model,
        "usage": cached.token_usage or {},
        "latency_ms": 0,
        "finish_reason": "cached",
        "content": json.dumps(cached.output_json),
        "usage_call_id": None,
    })()
    return ExecutedCall(
        output=output, result=result, reservation=None, quality=quality, quality_reason=reason,
        cached_analysis=cached,
    )


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
    input_hash = analysis_input_hash(prepared)
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


def _engine_of(session: Session | None):
    if session is None:
        return None
    bind = session.get_bind()
    return getattr(bind, "engine", bind)


def _charge_reservation(reservation: Any, tokens: int | None = None) -> None:
    cost = getattr(reservation, "cost", None) if reservation is not None else None
    note_spend(cost_usd=cost, tokens=tokens)


def persist_committed_part(
    session: Session | None, prepared: PreparedCall, executed: ExecutedCall,
) -> StructuredCallResult:
    """Commit one successful part immediately, then link the ledger row.

    The caller's transaction can roll back later (or the process can die)
    without losing the paid part. The ledger analysis_id is written only after
    this commit, so it never points at a row that does not exist.
    """
    from govcon.ai.usage_log import attach_call_ids

    if executed.cached_analysis is not None:
        analysis = executed.cached_analysis
        if session is not None and analysis not in session:
            analysis = session.merge(analysis)
        return StructuredCallResult(output=executed.output, analysis=analysis, prompt=prepared.prompt)
    analysis = build_analysis(prepared, executed)
    settings_blob = dict(analysis.generation_settings or {})
    if (prepared.context_manifest or {}).get("part") is not None:
        settings_blob["role"] = "part"
        settings_blob["part_index"] = prepared.context_manifest.get("part")
        analysis.generation_settings = settings_blob
    engine = _engine_of(session)
    call_id = getattr(executed.result, "usage_call_id", None)
    ids = [int(call_id)] if call_id is not None else []

    def _on_caller() -> StructuredCallResult:
        if session is None:
            return StructuredCallResult(output=executed.output, analysis=analysis, prompt=prepared.prompt)
        session.add(analysis)
        session.flush()
        if ids:
            attach_call_ids(session, ids, analysis_id=analysis.id, engine=engine)
        return StructuredCallResult(output=executed.output, analysis=analysis, prompt=prepared.prompt)

    if engine is None:
        return _on_caller()
    from sqlalchemy.orm import Session as OrmSession

    from govcon.models import Opportunity

    opportunity_id = prepared.opportunity_id
    if opportunity_id is not None:
        with OrmSession(engine) as probe:
            if probe.get(Opportunity, opportunity_id) is None:
                return _on_caller()
    with OrmSession(engine) as db, db.begin():
        db.add(analysis)
        db.flush()
        analysis_id = analysis.id
    attach_call_ids(session, ids, analysis_id=analysis_id, engine=engine)
    if session is not None:
        analysis = session.get(AIAnalysis, analysis_id) or analysis
    return StructuredCallResult(output=executed.output, analysis=analysis, prompt=prepared.prompt)


@trace_phase("ai.structured.persist_structured_result")
def persist_structured_result(
    session: Session | None, prepared: PreparedCall, executed: ExecutedCall
) -> StructuredCallResult:
    """Commit a paid part on its own connection; reuse a cached part as-is."""
    return persist_committed_part(session, prepared, executed)


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
