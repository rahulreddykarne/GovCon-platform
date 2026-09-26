"""Run one registry-managed prompt and persist a reproducible ``ai_analyses`` row.

Pipeline (§37.4): prompt registry → system prompt (shared fragments + task
prompt) → bounded user context (required variables) → provider call through
the AI gateway → JSON parse → Pydantic schema → persist with prompt name,
version, hash, schema version, settings, input hash, and context manifest.

Malformed output fails closed with ``StructuredCallError``; nothing is stored
as a valid analysis.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from govcon.ai.providers import NoProviderConfigured, get_provider
from govcon.ai.providers.deepseek import parse_json_response
from govcon.ai.schemas import SCHEMA_REGISTRY
from govcon.config import Settings, get_settings
from govcon.models import AIAnalysis
from govcon.prompting.loader import PromptAsset
from govcon.prompting.registry import load_prompt, load_prompt_from_disk
from govcon.prompting.renderer import PromptRenderError, render_system_prompt, render_user_context
from govcon.security.classification import DataClassification

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
    """Return the registry-active version when synced, else the source-controlled active file."""
    prompt_root = settings.resolved_prompt_root()
    if session is not None:
        try:
            return load_prompt(session, prompt_name, version="active", prompt_root=prompt_root)
        except (ValueError, FileNotFoundError):
            pass
    return load_prompt_from_disk(prompt_root, prompt_name)


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
    classification: DataClassification = DataClassification.PUBLIC,
) -> StructuredCallResult:
    settings = settings or get_settings()
    prompt = resolve_prompt(session, prompt_name, settings)
    schema_version = prompt.metadata.get("schema_version", "")
    schema_cls = SCHEMA_REGISTRY.get(schema_version)
    if schema_cls is None:
        raise StructuredCallError("schema_error", f"prompt {prompt_name} declares unknown schema {schema_version!r}")

    try:
        system_prompt = render_system_prompt(prompt, settings.resolved_prompt_root())
        user_prompt = render_user_context(prompt, variables)
    except PromptRenderError as exc:
        raise StructuredCallError("render_error", str(exc)) from exc

    try:
        provider = get_provider(settings, provider_name=provider_name)
    except NoProviderConfigured as exc:
        raise StructuredCallError("no_provider", str(exc)) from exc

    generation_settings = {"temperature": 0.0, "json_mode": True}
    if model:
        generation_settings["model"] = model
    attempts = 1 + max(0, int(settings.prompt_max_retries_on_invalid_json or 0))
    last_error: StructuredCallError | None = None
    for attempt in range(attempts):
        try:
            result = provider.complete(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=model,
                temperature=0.0,
                json_mode=True,
                classification=classification,
                purpose=prompt_name,
            )
        except Exception as exc:
            raise StructuredCallError("provider_error", f"{type(exc).__name__}: {exc}") from exc
        try:
            data = parse_json_response(result)
            validated = schema_cls.model_validate(data)
        except (json.JSONDecodeError, ValueError, ValidationError) as exc:
            last_error = StructuredCallError("invalid_output", f"attempt {attempt + 1}: {exc}")
            logger.warning("structured output rejected prompt=%s attempt=%d: %s", prompt_name, attempt + 1, exc)
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
            estimated_cost=None,
            latency_ms=getattr(result, "latency_ms", None),
        )
        if session is not None:
            session.add(analysis)
            session.flush()
        return StructuredCallResult(output=validated, analysis=analysis, prompt=prompt)

    assert last_error is not None
    raise last_error
