"""Solicitation analysis orchestrator.

Composes extracted text from downloaded attachments with opportunity metadata,
sends the combined context to the AI provider for structured analysis, validates
the response against the output schema, and persists the result to ``ai_analyses``.

AI errors never alter source data. Results are saved to ``ai_analyses``, not
directly over opportunity source fields.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.ai.analysis_types import AnalysisType
from govcon.config import Settings, get_settings
from govcon.models import AIAnalysis, Opportunity, StoredFile
from govcon.security.classification import DataClassification, strictest_classification
from govcon.workflow.source_revision import (
    SOURCE_REVISION_KEY,
    current_source_revision,
    is_stale,
    stamp_of,
)

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

    source_revision = current_source_revision(session, opportunity.id)
    if not force:
        existing = session.scalars(
            select(AIAnalysis)
            .where(
                AIAnalysis.opportunity_id == opportunity.id,
                AIAnalysis.analysis_type == AnalysisType.SOLICITATION_SUMMARY,
                AIAnalysis.schema_version == SCHEMA_VERSION,
            )
            .order_by(AIAnalysis.created_at.desc(), AIAnalysis.id.desc())
            .limit(1)
        ).first()
        if existing is not None and is_stale(stamp_of(existing.context_manifest), source_revision):
            logger.info(
                "cached solicitation analysis %d is stale for opportunity %d; re-running",
                existing.id,
                opportunity.id,
            )
            existing = None
        if existing is not None:
            logger.info(
                "solicitation analysis already exists for opportunity %d",
                opportunity.id,
            )
            return existing

    # The current attachment set only (the same rows the inventory and the
    # source revision use); removed or replaced versions are history.
    files = session.execute(
        select(StoredFile).where(
            StoredFile.opportunity_id == opportunity.id,
            StoredFile.active.is_(True),
            StoredFile.extraction_status.in_(["success", "partial"]),
        )
    ).scalars().all()

    if not files:
        logger.info("no extracted text for opportunity %d", opportunity.id)
        return None

    context_manifest = _build_context_manifest(opportunity, files)
    context_manifest[SOURCE_REVISION_KEY] = source_revision
    omitted_sources: list[dict] = []
    source_context = _build_user_prompt(opportunity, files,
        byte_budget=min(settings.ai_max_input_tokens_per_call, settings.ai_max_input_tokens_per_opportunity) // 2,
        omitted_sources=omitted_sources)
    context_manifest["omitted_sources"] = omitted_sources
    context_manifest["warnings"] = ([{"code": "context_truncated", "severity": "high",
        "message": "Source text was omitted from this summary; omitted documents remain unreviewed."}] if omitted_sources else [])
    from govcon.ai.structured import StructuredCallError, run_structured_prompt
    try:
        result = run_structured_prompt(
            session,
            opportunity_id=opportunity.id,
            prompt_name="solicitation_analysis",
            analysis_type=AnalysisType.SOLICITATION_SUMMARY,
            variables={
                "OPPORTUNITY_JSON": json.dumps({
                    "id": opportunity.id, "source": opportunity.source,
                    "source_id": opportunity.source_id, "title": opportunity.title,
                    "response_deadline": opportunity.response_deadline,
                }, default=str),
                "SOURCE_PACKAGE_JSON": source_context,
            },
            context_manifest=context_manifest,
            settings=settings,
            classification=strictest_classification(*(f.classification for f in files)),
        )
    except StructuredCallError as exc:
        if exc.reason == "no_provider":
            import warnings
            warnings.warn("No AI provider configured; solicitation analysis skipped", AnalysisWarning, stacklevel=2)
        logger.warning("solicitation analysis refused for opportunity %d: %s", opportunity.id, exc.reason)
        return None
    analysis = result.analysis
    if omitted_sources:
        # Keep the omission visible in the user-facing structured result as
        # well as the provenance manifest; it cannot imply complete review.
        analysis.output_json = {**analysis.output_json, "missing_information": [
            *analysis.output_json.get("missing_information", []),
            {"field": "source_package", "reason": "Configured input limit omitted source text.", "impact": "Incomplete summary; review omitted sources separately."}]}
    analysis.source_refs = analysis.output_json.get("source_refs") or None
    session.flush()
    return analysis


def _build_user_prompt(opp: Opportunity, files: list[StoredFile], *, byte_budget: int = 24_000, omitted_sources: list[dict] | None = None) -> str:
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
    remaining = max(0, byte_budget - len("\n".join(parts).encode("utf-8")))
    for f in files:
        if f.extracted_text:
            header = f"\n### [{f.filename}] (file_id={f.id})"
            encoded = f.extracted_text.encode("utf-8")
            allowance = max(0, remaining - len(header.encode("utf-8")) - 2)
            excerpt = encoded[:allowance].decode("utf-8", errors="ignore")
            if excerpt:
                parts.extend([header, excerpt])
                remaining -= len(header.encode("utf-8")) + len(excerpt.encode("utf-8")) + 2
            if len(excerpt.encode("utf-8")) < len(encoded) and omitted_sources is not None:
                omitted_sources.append({"file_id": f.id, "filename": f.filename, "original_bytes": len(encoded), "included_bytes": len(excerpt.encode("utf-8"))})

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
                "classification": f.classification,
                "source_origin": f.source_origin,
            }
            for f in files
        ],
        "structured_inputs": {},
    }
