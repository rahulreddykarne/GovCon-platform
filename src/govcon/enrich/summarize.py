"""Solicitation analysis orchestrator.

Composes extracted text from downloaded attachments with opportunity metadata,
sends the combined context to the AI provider for structured analysis, validates
the response against the output schema, and persists the result to ``ai_analyses``.

AI errors never alter source data. Results are saved to ``ai_analyses``, not
directly over opportunity source fields.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.ai.providers import NoProviderConfigured, get_provider
from govcon.ai.providers.deepseek import DeepSeekResult, parse_json_response
from govcon.ai.schemas import SolicitationAnalysisV1, validate_analysis_output
from govcon.config import Settings, get_settings
from govcon.models import AIAnalysis, Opportunity, StoredFile
from govcon.prompting.registry import load_prompt_from_disk
from govcon.prompting.renderer import render_system_prompt
from govcon.security.classification import DataClassification

logger = logging.getLogger("govcon.enrich.summarize")

SCHEMA_VERSION = "solicitation_analysis.v1"


class AnalysisWarning(UserWarning):
    """Non-fatal issue during analysis (e.g. missing API key)."""


def run_solicitation_analysis(
    session: Session,
    opportunity: Opportunity,
    *,
    settings: Settings | None = None,
    force: bool = False,
) -> AIAnalysis | None:
    """Run structured solicitation analysis on an opportunity's attachments.

    Returns the persisted ``AIAnalysis`` row, or ``None`` if:
    - No AI provider is configured (logs a warning)
    - No extracted text is available
    - Analysis already exists and ``force`` is False

    AI errors are logged but never alter source data.
    """
    settings = settings or get_settings()

    if not force:
        existing = session.execute(
            select(AIAnalysis).where(
                AIAnalysis.opportunity_id == opportunity.id,
                AIAnalysis.analysis_type == "solicitation_summary",
                AIAnalysis.schema_version == SCHEMA_VERSION,
            )
        ).scalar_one_or_none()
        if existing is not None:
            logger.info(
                "solicitation analysis already exists for opportunity %d",
                opportunity.id,
            )
            return existing

    files = session.execute(
        select(StoredFile).where(
            StoredFile.opportunity_id == opportunity.id,
            StoredFile.extraction_status.in_(["success", "partial"]),
        )
    ).scalars().all()

    if not files:
        logger.info("no extracted text for opportunity %d", opportunity.id)
        return None

    try:
        provider = get_provider(settings)
    except NoProviderConfigured as exc:
        import warnings
        warnings.warn(
            f"No AI provider configured — solicitation analysis skipped: {exc}",
            AnalysisWarning,
            stacklevel=2,
        )
        logger.warning("no AI provider configured: %s", exc)
        return None

    prompt_root = settings.resolved_prompt_root()
    prompt_asset = load_prompt_from_disk(prompt_root, "solicitation_analysis")
    system_prompt = render_system_prompt(prompt_asset, prompt_root)

    user_prompt = _build_user_prompt(opportunity, files)

    context_manifest = _build_context_manifest(opportunity, files)
    input_hash = hashlib.sha256(
        json.dumps(context_manifest, sort_keys=True).encode()
    ).hexdigest()

    try:
        result: DeepSeekResult = provider.complete(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.0,
            json_mode=True,
            classification=DataClassification.PUBLIC,
            purpose="solicitation_analysis",
        )
    except Exception as exc:
        logger.error("AI provider call failed for opportunity %d: %s", opportunity.id, exc)
        return None

    try:
        output_data = parse_json_response(result)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("failed to parse AI JSON response: %s", exc)
        return None

    try:
        validated = validate_analysis_output(SCHEMA_VERSION, output_data)
        output_json = validated.model_dump(mode="json")
    except Exception as exc:
        logger.error("AI output schema validation failed: %s", exc)
        if settings.prompt_fail_on_schema_error:
            return None
        output_json = output_data

    source_refs = output_json.get("source_refs", [])

    analysis = AIAnalysis(
        opportunity_id=opportunity.id,
        analysis_type="solicitation_summary",
        provider=result.provider,
        model=result.model,
        prompt_name=prompt_asset.name,
        prompt_version=prompt_asset.version,
        prompt_hash=prompt_asset.content_hash,
        schema_version=SCHEMA_VERSION,
        generation_settings={"temperature": 0.0, "json_mode": True},
        input_snapshot_hash=input_hash,
        context_manifest=context_manifest,
        output_json=output_json,
        source_refs=source_refs if source_refs else None,
        token_usage=result.usage or None,
        estimated_cost=None,
        latency_ms=result.latency_ms,
    )
    session.add(analysis)
    session.flush()
    logger.info(
        "solicitation analysis saved for opportunity %d (ai_analyses.id=%d)",
        opportunity.id,
        analysis.id,
    )
    return analysis


def _build_user_prompt(opp: Opportunity, files: list[StoredFile]) -> str:
    """Compose the user message from opportunity metadata and extracted text."""
    parts: list[str] = []

    parts.append("## Opportunity Metadata")
    meta = {
        "id": opp.id,
        "source": opp.source,
        "source_id": opp.source_id,
        "title": opp.title,
        "agency": opp.agency_path,
        "psc_code": opp.psc_code,
        "naics_code": opp.naics_code,
        "nsn": opp.nsn,
        "set_aside_code": opp.set_aside_code,
        "response_deadline": str(opp.response_deadline) if opp.response_deadline else None,
        "status": opp.status,
    }
    parts.append(json.dumps(meta, indent=2, default=str))

    parts.append("\n## Document Inventory")
    for f in files:
        parts.append(
            f"- File ID {f.id}: {f.filename} ({f.mime_type}), "
            f"SHA-256: {f.sha256}, extraction: {f.extraction_status}"
        )

    parts.append("\n## Extracted Source Content")
    for f in files:
        if f.extracted_text:
            text = f.extracted_text
            parts.append(f"\n### [{f.filename}] (file_id={f.id})")
            parts.append(text[:200_000])

    return "\n".join(parts)


def _build_context_manifest(opp: Opportunity, files: list[StoredFile]) -> dict:
    """Build the context manifest for reproducibility."""
    return {
        "opportunity_id": opp.id,
        "source_snapshots": [],
        "files": [
            {
                "file_id": f.id,
                "sha256": f.sha256,
                "filename": f.filename,
                "extraction_status": f.extraction_status,
            }
            for f in files
        ],
        "structured_inputs": {},
    }
