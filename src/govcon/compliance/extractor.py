"""Requirement extraction (§15.3–15.5).

Layer 1 only: what does the government require? Nothing here judges
compliance.

- Pass A: ``requirement_extraction_a`` over documents in package order,
  page-labeled chunks.
- Pass B: ``requirement_extraction_b`` over a different context strategy —
  amendments first, then tables/attachments/pricing/forms, Q&A, SOW, and the
  base solicitation last, chunked by table/paragraph blocks. Pass B may use a
  different provider/model (``COMPLIANCE_PASS_B_*``) and escalates to
  ``COMPLIANCE_ESCALATION_*`` for high-value opportunities.
- Pass D: a deterministic mandatory-language/table scanner that never calls a
  model. It is a recall safety net; it never makes anything satisfied.

Every candidate's supporting quote is checked against the cited source text.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from govcon.ai.analysis_types import AnalysisType
from govcon.ai.providers import resolve_provider_model
from govcon.ai.structured import (
    StructuredCallError,
    checked_output,
    run_structured_prompt,
)
from govcon.compliance.matrix import record_run
from govcon.compliance.records import (
    REQUIREMENT_TYPES,
    Candidate,
    Inventory,
    SourceDocument,
)
from govcon.compliance.schemas import RequirementExtractionV1
from govcon.compliance.text import (
    classify_type,
    estimate_severity,
    extract_key_values,
    find_clause_references,
    is_mandatory_language,
    mandatory_flag,
    normalize_ws,
    quote_in_text,
    split_sentences,
)
from govcon.config import Settings, get_settings
from govcon.diagnostics import trace_phase
from govcon.documents.chunking import AdaptivePlanner, split_text_batch
from govcon.models import Opportunity

logger = logging.getLogger("govcon.compliance.extractor")

EXTRACTOR_VERSION = "requirement_extraction.v1"
SCANNER_VERSION = "deterministic_scanner.v1"
PASS_PROMPTS = {"A": "requirement_extraction_a", "B": "requirement_extraction_b"}
_B_ORDER = {"amendment": 0, "pricing_sheet": 1, "form": 1, "attachment": 1, "drawing_spec": 1, "qa": 2, "sow_pws": 3, "solicitation": 4}
_HEADING = re.compile(r"^(?:section\s+)?(?:[A-Z]\.|[A-Z]?\d+(?:\.\d+)*\.?)\s+[A-Z][A-Za-z0-9 ,/&()'-]{2,70}$|^[A-Z][A-Z0-9 ,/&()'-]{4,70}$")
_CHARS_PER_TOKEN = 4


@dataclass
class PassOutcome:
    pass_label: str
    candidates: list[Candidate]
    status: str
    run_id: int | None = None
    analysis_id: int | None = None
    provider: str | None = None
    model: str | None = None
    warnings: list[dict[str, Any]] = field(default_factory=list)


# ── context strategies ──


def _chunk_header(doc: SourceDocument, page: int | None) -> str:
    return (
        f"[file_id={doc.file_id} page={page if page is not None else 'unknown'} "
        f"document_type={doc.document_type} filename={doc.filename} snapshot_id={doc.snapshot_id}]"
    )


def build_context(inventory: Inventory, strategy: str, *, char_budget: int) -> list[dict[str, Any]]:
    """Return labeled source chunks for one pass. Source IDs are always preserved."""
    chunks: list[dict[str, Any]] = []
    if strategy == "A":
        docs = sorted(inventory.documents, key=lambda d: (d.precedence_rank, d.file_id or 0))
        for doc in docs:
            for page, text in doc.pages():
                if text and text.strip():
                    chunks.append({"file_id": doc.file_id, "page": page, "text": f"{_chunk_header(doc, page)}\n{text}"})
    else:
        docs = sorted(
            inventory.documents,
            key=lambda d: (_B_ORDER.get(d.document_type, 1), -(d.amendment_number or 0), d.file_id or 0),
        )
        for doc in docs:
            for page, text in doc.pages():
                if not text or not text.strip():
                    continue
                blocks = re.split(r"\n(?=\[Table \d+\]|\[Sheet: )", text)
                tables = [b for b in blocks if b.startswith(("[Table", "[Sheet"))]
                prose = [b for b in blocks if not b.startswith(("[Table", "[Sheet"))]
                for block in tables + prose:
                    for start in range(0, len(block), 3000):
                        piece = block[start : start + 3000]
                        if piece.strip():
                            chunks.append({"file_id": doc.file_id, "page": page, "text": f"{_chunk_header(doc, page)}\n{piece}"})
    used = 0
    bounded: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks):
        if used + len(chunk["text"]) > char_budget:
            break
        used += len(chunk["text"])
        bounded.append({"chunk_id": f"{strategy}{index + 1}", **chunk})
    return bounded


def _inventory_json(inventory: Inventory) -> list[dict[str, Any]]:
    return [d.manifest() for d in inventory.documents]


def _amendment_json(inventory: Inventory) -> dict[str, Any]:
    return {
        "amendments": [
            {"file_id": d.file_id, "amendment_number": d.amendment_number, "document_date": d.document_date.isoformat() if d.document_date else None, "filename": d.filename}
            for d in inventory.amendments()
        ]
    }


# ── candidate mapping and citation checks ──


def candidates_from_output(pass_label: str, requirements: list[Any], inventory: Inventory) -> list[Candidate]:
    candidates: list[Candidate] = []
    known_files = inventory.by_file_id()
    for index, item in enumerate(requirements, start=1):
        text = normalize_ws(item.requirement_text)
        quote = (normalize_ws(item.supporting_quote) or None)
        if quote is not None:
            quote = quote[:200]
        requirement_type = item.requirement_type if item.requirement_type in REQUIREMENT_TYPES else classify_type(f"{text} {quote or ''}")
        key_values = {k: v for k, v in (item.normalized_values or {}).items() if v is not None}
        key_values.update(extract_key_values(quote or text))
        clauses = sorted({f"{r.family} {r.number}" for r in find_clause_references(f"{text} {quote or ''} {' '.join(item.clause_references)}")})
        file_id = item.source_file_id if item.source_file_id in known_files else None
        candidates.append(
            Candidate(
                candidate_id=f"{pass_label}-{index}",
                pass_label=pass_label,
                requirement_text=text,
                requirement_type=requirement_type,
                mandatory=item.mandatory,
                severity=item.severity,
                response_required=item.response_required,
                source_file_id=file_id,
                source_page=item.source_page,
                source_section=item.source_section,
                supporting_quote=quote,
                source_snapshot_id=item.source_snapshot_id or (known_files[file_id].snapshot_id if file_id in known_files else None),
                confidence=item.confidence,
                uncertainty_reason=item.uncertainty_reason if file_id is not None or item.source_file_id is None else f"cited unknown file_id {item.source_file_id}",
                key_values=key_values,
                clause_refs=clauses,
            )
        )
    verify_citations(candidates, inventory)
    return candidates


def verify_citations(candidates: list[Candidate], inventory: Inventory) -> None:
    """Mark whether each supporting quote is actually present in the cited source (page when known)."""
    docs = inventory.by_file_id()
    for candidate in candidates:
        doc = docs.get(candidate.source_file_id)
        if doc is None or not candidate.supporting_quote:
            candidate.citation_verified = False if candidate.supporting_quote or candidate.source_file_id else None
            continue
        # With per-page text a quote on the wrong page is an inaccurate citation.
        candidate.citation_verified = quote_in_text(candidate.supporting_quote, doc.page_text(candidate.source_page))


# ── deterministic scanner (pass D) ──


def scan_requirements(inventory: Inventory) -> list[Candidate]:
    """Find mandatory-language sentences and table rows; purely deterministic."""
    candidates: list[Candidate] = []
    seen: set[tuple[int | None, str]] = set()
    counter = 0
    for doc in sorted(inventory.documents, key=lambda d: (d.precedence_rank, d.file_id or 0)):
        for page, text in doc.pages():
            if not text:
                continue
            for section, block in _sections(text):
                for sentence in split_sentences(block):
                    key_values = extract_key_values(sentence)
                    decisive = {"page_limit", "delivery_days", "response_deadline_date", "required_count", "clin_quantities", "amendments_to_acknowledge"}
                    if not is_mandatory_language(sentence) and not (decisive & key_values.keys()):
                        continue
                    normalized = normalize_ws(sentence)
                    if (doc.file_id, normalized.lower()) in seen:
                        continue
                    seen.add((doc.file_id, normalized.lower()))
                    counter += 1
                    requirement_type = classify_type(normalized)
                    candidates.append(
                        Candidate(
                            candidate_id=f"D-{counter}",
                            pass_label="D",
                            requirement_text=normalized[:1000],
                            requirement_type=requirement_type,
                            mandatory=mandatory_flag(normalized),
                            severity=estimate_severity(normalized, requirement_type),
                            response_required=None,
                            source_file_id=doc.file_id,
                            source_page=page,
                            source_section=section,
                            supporting_quote=normalized[:500],
                            source_snapshot_id=doc.snapshot_id,
                            confidence=None,
                            key_values=key_values,
                            clause_refs=sorted({f"{r.family} {r.number}" for r in find_clause_references(normalized)}),
                            citation_verified=True,
                        )
                    )
    return candidates


def structural_candidates(inventory: Inventory) -> list[Candidate]:
    """Requirements implied by the package structure itself (one per amendment)."""
    candidates: list[Candidate] = []
    for index, doc in enumerate(inventory.amendments(), start=1):
        if not doc.amendment_number:
            continue
        number = f"{doc.amendment_number:04d}"
        quote = None
        for page, text in doc.pages():
            for line in (text or "").splitlines():
                if re.search(r"amendment", line, re.I) and len(line.strip()) >= 8:
                    quote = normalize_ws(line)[:300]
                    source_page = page
                    break
            if quote:
                break
        if quote is None:
            continue
        candidates.append(
            Candidate(
                candidate_id=f"D-amend-{index}",
                pass_label="D",
                requirement_text=f"Acknowledge receipt of Amendment {number} in the offer.",
                requirement_type="amendment_acknowledgment",
                mandatory=True,
                severity="critical",
                source_file_id=doc.file_id,
                source_page=source_page,
                supporting_quote=quote,
                source_snapshot_id=doc.snapshot_id,
                key_values={"amendments_to_acknowledge": [number]},
                citation_verified=True,
            )
        )
    return candidates


def _sections(text: str) -> list[tuple[str | None, str]]:
    sections: list[tuple[str | None, str]] = []
    heading: str | None = None
    buffer: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and _HEADING.match(stripped) and not is_mandatory_language(stripped) and "\t" not in stripped:
            if buffer:
                sections.append((heading, "\n".join(buffer)))
                buffer = []
            heading = stripped[:120]
            continue
        buffer.append(line)
    if buffer:
        sections.append((heading, "\n".join(buffer)))
    return sections


# ── AI passes ──


def _pass_provider(pass_label: str, opportunity: Opportunity, settings: Settings) -> tuple[str | None, str | None, list[dict[str, Any]]]:
    warnings: list[dict[str, Any]] = []
    if pass_label == "A":
        return None, None, warnings
    provider = settings.compliance_pass_b_provider
    model = settings.compliance_pass_b_model
    threshold = settings.compliance_escalation_min_value
    value = float(opportunity.estimated_value_max) if opportunity.estimated_value_max is not None else None
    if threshold is not None and value is not None and value >= threshold:
        if settings.compliance_escalation_provider:
            provider, model = settings.compliance_escalation_provider, settings.compliance_escalation_model
        else:
            warnings.append({"code": "escalation_unconfigured", "severity": "medium", "message": "High-value opportunity but no COMPLIANCE_ESCALATION_PROVIDER is configured; Pass B uses the default provider."})
    warnings += independence_warnings(settings, provider, model, code="pass_b_not_independent", what="Pass B")
    return provider, model, warnings


def independence_warnings(
    settings: Settings, provider: str | None, model: str | None, *, code: str, what: str
) -> list[dict[str, Any]]:
    """Warn when a 'second opinion' resolves to the primary provider and model.

    Two calls to the same model share its blind spots, so agreement between
    them is weak evidence that nothing was missed.
    """
    primary = resolve_provider_model(settings)
    second = resolve_provider_model(settings, provider_name=provider, model=model)
    if second != primary:
        return []
    return [{
        "code": code,
        "severity": "medium",
        "message": (
            f"{what} resolves to the same provider and model as the primary pass "
            f"({primary[0]}/{primary[1]}); set a different provider or model to get an independent check."
        ),
    }]


def _batches(chunks: list[dict[str, Any]], byte_budget: int) -> list[list[dict[str, Any]]]:
    """Order-preserving groups of chunks whose text fits ``byte_budget`` UTF-8 bytes."""
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    used = 0
    for chunk in chunks:
        size = len(chunk["text"].encode("utf-8")) + 2
        if current and used + size > byte_budget:
            batches.append(current)
            current, used = [], 0
        current.append(chunk)
        used += size
    if current:
        batches.append(current)
    return batches


def _gaps(chunks: list[dict[str, Any]], inventory: Inventory, reason: str) -> list[dict[str, Any]]:
    names = {d.file_id: d.filename for d in inventory.documents}
    gaps: dict[Any, dict[str, Any]] = {}
    for chunk in chunks:
        gap = gaps.setdefault(chunk["file_id"], {"file_id": chunk["file_id"], "filename": names.get(chunk["file_id"]),
                                                 "pages": [], "reason": reason})
        page = chunk["page"] if chunk["page"] is not None else "document"
        if page not in gap["pages"]:
            gap["pages"].append(page)
    return list(gaps.values())


# Failures confined to one part's answer; the pass moves on to the next part.
_PART_LOCAL_FAILURES = frozenset({"invalid_output", "output_truncated", "provider_error"})


@trace_phase("compliance.extractor.run_ai_pass")
def run_ai_pass(
    session: Session,
    opportunity: Opportunity,
    inventory: Inventory,
    pass_label: str,
    *,
    settings: Settings | None = None,
) -> PassOutcome:
    """Run one extraction pass over the whole document set (ADR-066).

    Chunks are sent in as many calls as the per-call limit needs. If the
    per-opportunity budget (or the provider) stops the pass part-way, the
    chunks not sent are recorded as coverage gaps and the pass is
    ``incomplete``; it is never reported complete.
    """
    settings = settings or get_settings()
    prompt_name = PASS_PROMPTS[pass_label]
    chunks = build_context(inventory, pass_label, char_budget=10**12)
    planner = AdaptivePlanner(
        list(chunks), units=1, max_units=2,
        output_cap=settings.ai_max_output_tokens_per_call,
        byte_budget=settings.ai_source_batch_bytes,
    )
    provider_name, model, warnings = _pass_provider(pass_label, opportunity, settings)
    base_variables: dict[str, Any] = {
        "DOCUMENT_INVENTORY_JSON": _inventory_json(inventory),
        "AMENDMENT_JSON": _amendment_json(inventory),
    }
    if pass_label == "A":
        base_variables["OPPORTUNITY_JSON"] = {
            "id": opportunity.id,
            "source": opportunity.source,
            "solicitation_number": opportunity.solicitation_number,
            "title": opportunity.title,
            "agency": opportunity.agency_path,
            "psc_code": opportunity.psc_code,
            "naics_code": opportunity.naics_code,
            "set_aside_code": opportunity.set_aside_code,
            "response_deadline": opportunity.response_deadline.isoformat() if opportunity.response_deadline else None,
        }
    source_snapshot_ids = sorted({d.snapshot_id for d in inventory.documents if d.snapshot_id is not None})
    base_manifest = {
        "opportunity_id": opportunity.id,
        "strategy": pass_label,
        "source_snapshots": source_snapshot_ids,
        "files": [{"file_id": d.file_id, "sha256": d.sha256, "pages": d.page_count, "classification": d.classification, "source_origin": d.source_origin} for d in inventory.documents],
    }
    run_type = f"extraction_pass_{pass_label.lower()}"
    from govcon.security.classification import (
        has_sendable_content,
        payload_classification,
    )

    classification = payload_classification(*inventory.documents)
    empty_gaps: list[dict[str, Any]] = [
        {
            "file_id": doc.file_id,
            "filename": doc.filename,
            "pages": [],
            "reason": "download_failed" if doc.text_extraction_status == "download_failed" else "no_extracted_content",
        }
        for doc in inventory.documents
        if not has_sendable_content(doc)
    ]
    if empty_gaps:
        warnings.append({
            "code": "source_ingestion_incomplete",
            "severity": "high",
            "message": (
                "Listed attachments with no extracted content were excluded from this pass and "
                "from classification: "
                + "; ".join(
                    f"{gap['filename'] or gap['file_id']} ({gap['reason']})" for gap in empty_gaps
                )
                + ". Re-download or ingest readable copies; they remain a coverage gap."
            ),
        })
    if not chunks:
        # No document text (nothing downloaded, or nothing readable): no call is made,
        # and the pass is recorded as failed so the extraction stays incomplete.
        warnings.append({"code": f"pass_{pass_label.lower()}_no_source_text", "severity": "high", "message": (
            f"Extraction pass {pass_label} did not run: the opportunity has no readable document text. "
            "Download or ingest the solicitation documents, then re-run compliance.")})
        run = record_run(
            session,
            opportunity_id=opportunity.id,
            run_type=run_type,
            run_version=EXTRACTOR_VERSION,
            output={"error": "no_source_text", "manifest": {**base_manifest, "part": 0, "parts": 0, "chunks": []},
                    "coverage": {"chunks_total": 0, "chunks_sent": 0, "gaps": empty_gaps}},
            status="failed",
            warnings=warnings,
            source_snapshot_ids=source_snapshot_ids,
        )
        return PassOutcome(pass_label, [], "failed", run.id, warnings=warnings)
    results = []
    gaps: list[dict[str, Any]] = list(empty_gaps)
    sent = 0
    last_error: StructuredCallError | None = None
    part_no = 0
    from govcon.ai.usage_log import attach_call_ids, collect_call_ids, stop_collecting
    link_token, linked_ids = collect_call_ids()
    try:
        while True:
            batch = planner.next_batch()
            if not batch:
                break
            part_no += 1
            manifest = {
                **base_manifest,
                "part": part_no,
                "chunks": [{"chunk_id": c["chunk_id"], "file_id": c["file_id"], "page": c["page"]} for c in batch],
            }
            try:
                result = run_structured_prompt(
                    session,
                    classification=classification,
                    opportunity_id=opportunity.id,
                    prompt_name=prompt_name,
                    analysis_type=AnalysisType.COMPLIANCE_REVIEW,
                    variables={**base_variables, "SOURCE_CHUNKS": "\n\n".join(c["text"] for c in batch)},
                    context_manifest=manifest,
                    settings=settings,
                    provider_name=provider_name,
                    model=model,
                )
                results.append(result)
                usage = (result.analysis.token_usage or {}) if result.analysis is not None else {}
                planner.consume(
                    len(batch),
                    output_tokens=int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
                    input_tokens=int(usage.get("prompt_tokens") or usage.get("input_tokens") or 1),
                )
                sent += len(batch)
            except StructuredCallError as exc:
                last_error = exc
                reason = "budget_exhausted" if exc.reason == "budget_exceeded" else exc.reason
                if exc.reason == "output_truncated":
                    halves = split_text_batch(batch)
                    if halves:
                        planner.pending[:len(batch)] = [item for half in halves for item in half]
                        planner.units = 1
                        part_no -= 1
                        continue
                if exc.reason in _PART_LOCAL_FAILURES:
                    gaps += _gaps(batch, inventory, reason)
                    planner.consume(len(batch), truncated=True)
                    continue
                gaps += _gaps(planner.pending, inventory, reason)
                break
    finally:
        stop_collecting(link_token)

    if not results:
        failure = last_error
        if failure is None:
            raise StructuredCallError("provider_error", "No extraction result or recorded failure")
        warnings.append({"code": f"pass_{pass_label.lower()}_{failure.reason}", "severity": "high", "message": f"Extraction pass {pass_label} produced no usable output: {failure.detail}"})
        run = record_run(
            session,
            opportunity_id=opportunity.id,
            run_type=run_type,
            run_version=EXTRACTOR_VERSION,
            output={"error": failure.reason, "detail": failure.detail, "manifest": {**base_manifest, "parts": part_no},
                    "coverage": {"chunks_total": len(chunks), "chunks_sent": 0,
                                 "gaps": empty_gaps + _gaps(chunks, inventory, failure.reason)}},
            status="failed",
            warnings=warnings,
            source_snapshot_ids=source_snapshot_ids,
        )
        attach_call_ids(session, linked_ids, compliance_run_id=run.id)
        return PassOutcome(pass_label, [], "failed", run.id, warnings=warnings)

    read_gaps = [gap for gap in gaps if gap.get("reason") not in {"download_failed", "no_extracted_content"}]
    if read_gaps:
        warnings.append({"code": "context_truncated", "severity": "high", "message": (
            f"Pass {pass_label} could not read part of the source set ({read_gaps[0]['reason']}): "
            + "; ".join(f"{g['filename'] or g['file_id']} pages {', '.join(str(p) for p in g['pages'])}" for g in read_gaps)
            + ". Requirements there are not extracted.")})
    candidates = [c for result in results for c in candidates_from_output(pass_label, checked_output(result.output, RequirementExtractionV1).requirements, inventory)]
    first = results[0]
    run = record_run(
        session,
        opportunity_id=opportunity.id,
        run_type=run_type,
        run_version=EXTRACTOR_VERSION,
        output={
            "ai_analysis_id": first.analysis.id,
            "ai_analysis_ids": [r.analysis.id for r in results],
            "prompt": {"name": first.prompt.name, "version": first.prompt.version, "hash": first.prompt.content_hash},
            "candidates": [c.__dict__ for c in candidates],
            "extraction_notes": [note for r in results for note in checked_output(r.output, RequirementExtractionV1).extraction_notes],
            "coverage": {"chunks_total": len(chunks), "chunks_sent": sent, "parts": part_no,
                         "parts_sent": len(results), "gaps": gaps},
        },
        status="incomplete" if gaps else "complete",
        warnings=warnings or None,
        source_snapshot_ids=source_snapshot_ids,
        input_hash=first.analysis.input_snapshot_hash if len(results) == 1 else hashlib.sha256(
            "".join(r.analysis.input_snapshot_hash or "" for r in results).encode()).hexdigest(),
    )
    attach_call_ids(session, linked_ids, compliance_run_id=run.id)
    return PassOutcome(
        pass_label, candidates, "incomplete" if gaps else "complete", run.id, first.analysis.id,
        first.analysis.provider, first.analysis.model, warnings,
    )


@trace_phase("compliance.extractor.run_scanner_pass")
def run_scanner_pass(session: Session, opportunity_id: int, inventory: Inventory) -> PassOutcome:
    candidates = scan_requirements(inventory) + structural_candidates(inventory)
    run = record_run(
        session,
        opportunity_id=opportunity_id,
        run_type="extraction_pass_d",
        run_version=SCANNER_VERSION,
        output={"candidate_count": len(candidates), "candidates": [c.__dict__ for c in candidates]},
        source_snapshot_ids=sorted({d.snapshot_id for d in inventory.documents if d.snapshot_id}),
    )
    return PassOutcome("D", candidates, "complete", run.id, provider="deterministic")
