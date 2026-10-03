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
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from govcon.ai.gateway import AIGatewayBlocked, authorize_external_call
from govcon.ai.budget import AIBudgetExceeded, complete_with_budget
from govcon.ai.providers import NoProviderConfigured, get_provider
from govcon.ai.providers.deepseek import parse_json_response
from govcon.ai.schemas import SCHEMA_REGISTRY
from govcon.config import Settings, get_settings
from govcon.models import AIAnalysis
from govcon.prompting.loader import PromptAsset
from govcon.prompting.registry import PromptRegistryAbsent, load_prompt, load_prompt_from_disk
from govcon.prompting.renderer import PromptRenderError, render_system_prompt, render_user_context
from govcon.security.classification import DataClassification, opportunity_classification

logger = logging.getLogger("govcon.ai.structured")


class StructuredCallError(RuntimeError):
    """A structured AI step did not produce valid, schema-conformant output."""

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


@dataclass(frozen=True)
class StructuredCallResult:
    output: BaseModel
    analysis: AIAnalysis | None
    prompt: PromptAsset


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
    settings = settings or get_settings()
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
    attempts = 1 + max(0, int(settings.prompt_max_retries_on_invalid_json or 0))
    last_error: StructuredCallError | None = None
    for attempt in range(attempts):
        try:
            result, reservation = complete_with_budget(provider, session,
                opportunity_id=opportunity_id,
                settings=settings,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=model,
                temperature=0.0,
                json_mode=True,
                classification=classification,
                purpose=prompt_name,
            )
        except AIBudgetExceeded as exc:
            raise StructuredCallError("budget_exceeded", str(exc)) from exc
        except AIGatewayBlocked as exc:
            raise StructuredCallError("blocked_by_policy", str(exc)) from exc
        except Exception as exc:
            raise StructuredCallError("provider_error", type(exc).__name__) from exc
        try:
            data = parse_json_response(result)
            validated = schema_cls.model_validate(data)
        except (json.JSONDecodeError, ValueError, ValidationError) as exc:
            last_error = StructuredCallError("invalid_output", f"attempt {attempt + 1}: {type(exc).__name__}")
            # Never log the provider response body; it can echo sensitive input.
            logger.warning("structured output rejected prompt=%s attempt=%d: %s", prompt_name, attempt + 1, type(exc).__name__)
            continue

        manifest = dict(context_manifest)
        manifest.setdefault("opportunity_id", opportunity_id)
        input_hash = hashlib.sha256(
            json.dumps({"variables": variables, "manifest": manifest}, sort_keys=True, default=str).encode()
        ).hexdigest()
        output_json = validated.model_dump(mode="json")
        analysis = AIAnalysis(
            opportunity_id=opportunity_id,
            analysis_type=analysis_type,
            provider=getattr(result, "provider", None) or getattr(provider, "name", None),
            model=getattr(result, "model", None) or model,
            prompt_name=prompt.name,
            prompt_version=prompt.version,
            prompt_hash=prompt.content_hash,
            schema_version=schema_version,
            generation_settings=generation_settings,
            input_snapshot_hash=input_hash,
            context_manifest=manifest,
            output_json=output_json,
            source_refs=None,
            token_usage=getattr(result, "usage", None) or None,
            estimated_cost=reservation.cost if reservation is not None else None,
            latency_ms=getattr(result, "latency_ms", None),
        )
        if session is not None:
            session.add(analysis)
            session.flush()
        return StructuredCallResult(output=validated, analysis=analysis, prompt=prompt)

    assert last_error is not None
    raise last_error
