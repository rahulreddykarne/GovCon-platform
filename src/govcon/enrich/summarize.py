"""Solicitation analysis orchestrator.

Composes extracted text from downloaded attachments with opportunity metadata,
sends it to the AI provider for structured analysis, validates the response
against the output schema, and persists the result to ``ai_analyses``.

The whole document set is analysed (ADR-066): page-cited chunks are sent in
as many calls as the per-call limit needs, and the parts are merged
deterministically. When the per-opportunity budget runs out, the files and
pages not analysed are recorded as gaps; the summary is never presented as
complete.

AI errors never alter source data. Results are saved to ``ai_analyses``, not
directly over opportunity source fields.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.ai.analysis_types import AnalysisType
from govcon.config import Settings, get_settings
from govcon.diagnostics import diagnostic_event, trace_phase
from govcon.documents.chunking import (
    Gap,
    SourceChunk,
    batch_chunks,
    chunks_for_pages,
    gaps_for,
    nbytes,
    render_batch,
    split_source_batch,
)
from govcon.models import AIAnalysis, FilePage, Opportunity, StoredFile
from govcon.security.classification import strictest_classification
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


@trace_phase("enrich.summarize.run_solicitation_analysis")
def run_solicitation_analysis(
    session: Session,
    opportunity: Opportunity,
    *,
    settings: Settings | None = None,
    force: bool = False,
    refusals: list[str] | None = None,
) -> AIAnalysis | None:
    """Run structured solicitation analysis on an opportunity's attachments.

    Returns the persisted ``AIAnalysis`` row, or ``None`` if:
    - No AI provider is configured (logs a warning)
    - No extracted text is available
    - Analysis already exists and ``force`` is False

    When it returns ``None`` without a cached analysis, the reason is appended
    to ``refusals`` (if given) so callers can show it.

    AI errors are logged but never alter source data.
    """
    from govcon.workflow.analysis_lock import hold_analysis_lock

    settings = settings or get_settings()
    with hold_analysis_lock(session, opportunity.id):
        return _run_solicitation_analysis(
            session, opportunity, settings=settings, force=force, refusals=refusals,
        )


def _run_solicitation_analysis(
    session: Session,
    opportunity: Opportunity,
    *,
    settings: Settings,
    force: bool,
    refusals: list[str] | None,
) -> AIAnalysis | None:
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
            diagnostic_event("summary.cache_miss", reason="stale", result_id=existing.id)
            logger.info(
                "cached solicitation analysis %d is stale for opportunity %d; re-running",
                existing.id,
                opportunity.id,
            )
            existing = None
        if existing is not None:
            from govcon.ai.schemas import SolicitationAnalysisV1

            try:
                SolicitationAnalysisV1.model_validate(existing.output_json)
            except ValueError:
                logger.info("cached solicitation analysis %d has no valid content; re-running", existing.id)
                existing = None
        if existing is not None and (existing.generation_settings or {}).get("quality") == "incomplete":
            logger.info("cached solicitation analysis %d is incomplete; re-running", existing.id)
            existing = None
        if existing is not None:
            logger.info(
                "solicitation analysis already exists for opportunity %d",
                opportunity.id,
            )
            diagnostic_event("summary.cache_hit", cached=True, result_id=existing.id)
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
        diagnostic_event("summary.skipped", reason="no_extracted_files", files=0)
        logger.info("no extracted text for opportunity %d", opportunity.id)
        if refusals is not None:
            refusals.append("no document text is available")
        return None

    context_manifest = _build_context_manifest(opportunity, files)
    context_manifest[SOURCE_REVISION_KEY] = source_revision
    chunks = _source_chunks(session, files)
    if not chunks:
        diagnostic_event("summary.skipped", reason="no_readable_text", files=len(files))
        logger.info("no readable source text for opportunity %d", opportunity.id)
        if refusals is not None:
            refusals.append(
                "no readable text was extracted; enable OCR and re-run preparation, "
                "or import a readable copy of the solicitation"
            )
        return None
    header = _metadata_block(opportunity, files)
    budget = min(settings.ai_max_input_tokens_per_call // 2, settings.ai_max_input_tokens_per_opportunity // 2,
                 settings.ai_source_batch_bytes)
    batches = batch_chunks(chunks, max(budget - nbytes(header) - 200, 2_000))
    diagnostic_event("summary.batches", files=len(files), parts=len(batches), candidates=len(chunks))
    classification = strictest_classification(*(f.classification for f in files))
    from govcon.ai.structured import (
        StructuredCallError,
        execute_prepared_call,
        prepare_structured_call,
    )
    from govcon.ai.usage_log import attach_call_ids, collect_call_ids, stop_collecting

    calls: list[tuple[Any, Any]] = []
    gaps: list[Gap] = []
    pending = list(batches)
    index = 0
    sent = 0
    link_token, linked_ids = collect_call_ids()
    try:
        while index < len(pending):
            batch = pending[index]
            diagnostic_event("summary.batch_start", part=index + 1, parts=len(pending), candidates=len(batch))
            part = (
                f" (part {index + 1} of {len(pending)}; other parts are analysed separately)"
                if len(pending) > 1 else ""
            )
            source = f"{header}\n\n## Extracted Source Content{part}\n{render_batch(batch)}"
            try:
                prepared = prepare_structured_call(
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
                        "SOURCE_PACKAGE_JSON": source,
                    },
                    context_manifest=context_manifest,
                    settings=settings,
                    classification=classification,
                )
                executed = execute_prepared_call(prepared, settings=settings, session=session)
            except StructuredCallError as exc:
                diagnostic_event("summary.batch_refused", level=logging.WARNING, reason=exc.reason,
                                 part=index + 1, parts=len(pending))
                if exc.reason == "output_truncated":
                    halves = split_source_batch(batch)
                    if halves:
                        pending[index:index + 1] = halves
                        continue
                if not calls:
                    if exc.reason == "no_provider":
                        import warnings
                        warnings.warn("No AI provider configured; solicitation analysis skipped", AnalysisWarning, stacklevel=2)
                    logger.warning("solicitation analysis refused for opportunity %d: %s", opportunity.id, exc.reason)
                    if refusals is not None:
                        refusals.append(f"{exc.reason}: {exc.detail}")
                    return None
                reason = "budget_exhausted" if exc.reason == "budget_exceeded" else exc.reason
                if exc.reason == "output_truncated":
                    gaps.extend(gaps_for(batch, reason))
                    index += 1
                    continue
                for rest in pending[index:]:
                    gaps.extend(gaps_for(rest, reason))
                logger.warning("solicitation analysis for opportunity %d stopped at part %d: %s",
                               opportunity.id, index + 1, exc.reason)
                break
            calls.append((prepared, executed))
            sent += len(batch)
            index += 1
            diagnostic_event("summary.batch_complete", part=index, quality=executed.quality)

        merged = merge_summaries([executed.output.model_dump(mode="json") for _, executed in calls])
        context_manifest["coverage"] = {
            "chunks_total": len(chunks), "chunks_sent": sent, "parts": len(pending),
            "parts_sent": len(calls), "gaps": [gap.as_dict() for gap in gaps],
        }
        # Kept under its earlier name for consumers that read omitted sources.
        context_manifest["omitted_sources"] = [gap.as_dict() for gap in gaps]
        context_manifest["warnings"] = ([{"code": "context_truncated", "severity": "high",
            "message": "Part of the source set was not analysed; the listed files and pages remain unreviewed."}] if gaps else [])
        if gaps:
            # Keep the gap visible in the user-facing result as well as the
            # provenance manifest; it cannot imply complete review.
            merged["missing_information"] = [*merged.get("missing_information", []), {
                "field": "source_package",
                "reason": "Not analysed: " + "; ".join(
                    f"{g.filename or g.file_id} pages {', '.join(str(p) for p in g.pages)} ({g.reason})" for g in gaps),
                "impact": "Incomplete summary; review these pages separately.",
            }]
        analysis = _merged_analysis(calls, merged, context_manifest)
        session.add(analysis)
        analysis.source_refs = analysis.output_json.get("source_refs") or None
        session.flush()
        attach_call_ids(session, linked_ids, analysis_id=analysis.id)
        return analysis
    finally:
        stop_collecting(link_token)


def _source_chunks(session: Session, files: Sequence[StoredFile]) -> list[SourceChunk]:
    """Cited chunks for every file: stored pages when present, else the whole text."""
    pages: dict[int, list[FilePage]] = {}
    for page in session.scalars(
        select(FilePage).where(FilePage.file_id.in_([f.id for f in files])).order_by(FilePage.file_id, FilePage.page_no)
    ):
        pages.setdefault(page.file_id, []).append(page)
    chunks: list[SourceChunk] = []
    for f in files:
        stored = pages.get(f.id)
        if stored:
            is_pdf = (f.mime_type or "") == "application/pdf" or (f.filename or "").lower().endswith(".pdf")
            triples = [(p.page_no if is_pdf else None, p.label, p.text) for p in stored]
        else:
            triples = [(None, "document", f.extracted_text or "")]
        chunks.extend(chunks_for_pages(f.id, f.filename, triples))
    return chunks


def merge_summaries(outputs: list[dict[str, Any]]) -> dict[str, Any]:
    """Combine per-part summaries without inventing anything.

    Lists are concatenated without duplicates. A single-valued section keeps
    the first part's answer; a different answer from a later part is recorded
    under ``conflicts`` rather than silently dropped.
    """
    if not outputs:
        return {}
    if len(outputs) == 1:
        return outputs[0]
    from govcon.ai.schemas import SolicitationAnalysisV1

    merged: dict[str, Any] = {}
    disagreements: list[str] = []
    for name, info in SolicitationAnalysisV1.model_fields.items():
        values = [out.get(name) for out in outputs]
        if info.default_factory is list:
            seen: set[str] = set()
            items: list[Any] = []
            for value in values:
                for item in value or []:
                    key = json.dumps(item, sort_keys=True, default=str)
                    if key not in seen:
                        seen.add(key)
                        items.append(item)
            merged[name] = items
        elif name == "summary":
            texts = [v.strip() for v in values if isinstance(v, str) and v.strip()]
            merged[name] = "\n\n".join(dict.fromkeys(texts)) or None
        else:
            present = [(i, v) for i, v in enumerate(values, start=1) if v not in (None, {}, [])]
            merged[name] = present[0][1] if present else None
            for part, value in present[1:]:
                if json.dumps(value, sort_keys=True, default=str) != json.dumps(present[0][1], sort_keys=True, default=str):
                    disagreements.append(
                        f"Parts {present[0][0]} and {part} of the source set disagree on {name}; review the cited pages."
                    )
    merged["conflicts"] = [*merged.get("conflicts", []), *disagreements]
    return merged


def _merged_analysis(calls: list[tuple[Any, Any]], merged: dict[str, Any], manifest: dict[str, Any]) -> AIAnalysis:
    """One ``ai_analyses`` row for all parts, with summed usage and cost."""
    from dataclasses import replace
    from types import SimpleNamespace

    from govcon.ai.schemas import SolicitationAnalysisV1
    from govcon.ai.structured import ExecutedCall, build_analysis

    first_prepared, first_executed = calls[0]
    usage: dict[str, int] = {}
    latency = 0
    costs = []
    for _, executed in calls:
        for key, value in (getattr(executed.result, "usage", None) or {}).items():
            if isinstance(value, int) and not isinstance(value, bool):
                usage[key] = usage.get(key, 0) + value
        latency += getattr(executed.result, "latency_ms", None) or 0
        if executed.reservation is not None and executed.reservation.cost is not None:
            costs.append(executed.reservation.cost)
    result = SimpleNamespace(
        provider=getattr(first_executed.result, "provider", None),
        model=getattr(first_executed.result, "model", None),
        usage=usage or None,
        latency_ms=latency or None,
    )
    prepared = replace(
        first_prepared,
        context_manifest=manifest,
        variables={"parts": [p.variables.get("SOURCE_PACKAGE_JSON") for p, _ in calls]},
    )
    output = SolicitationAnalysisV1.model_validate(merged)
    from govcon.ai.quality import assess_output_quality

    quality, reason = assess_output_quality(output)
    for _, executed in calls:
        if executed.quality == "incomplete":
            quality = "incomplete"
            reason = executed.quality_reason or reason or "A source part returned sparse output."
            break
    executed = ExecutedCall(
        output=output,
        result=result,
        reservation=SimpleNamespace(cost=sum(costs)) if costs else None,
        quality=quality,
        quality_reason=reason,
    )
    return build_analysis(prepared, executed)


def _metadata_block(opp: Opportunity, files: Sequence[StoredFile]) -> str:
    """Opportunity metadata and the document inventory, sent with every part."""
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
    lines = ["## Opportunity Metadata", json.dumps(meta, indent=2, default=str), "\n## Document Inventory"]
    lines.extend(
        f"- File ID {f.id}: {f.filename} ({f.mime_type}), SHA-256: {f.sha256}, extraction: {f.extraction_status}"
        for f in files
    )
    return "\n".join(lines)


def _build_context_manifest(opp: Opportunity, files: Sequence[StoredFile]) -> dict:
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
