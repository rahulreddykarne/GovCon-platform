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

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from govcon.ai.structured import StructuredCallError, run_structured_prompt
from govcon.compliance.matrix import record_run
from govcon.compliance.records import REQUIREMENT_TYPES, Candidate, Inventory, SourceDocument
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
        quote = normalize_ws(item.supporting_quote) or None
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
            return settings.compliance_escalation_provider, settings.compliance_escalation_model, warnings
        warnings.append({"code": "escalation_unconfigured", "severity": "medium", "message": "High-value opportunity but no COMPLIANCE_ESCALATION_PROVIDER is configured; Pass B uses the default provider."})
    return provider, model, warnings


def run_ai_pass(
    session: Session,
    opportunity: Opportunity,
    inventory: Inventory,
    pass_label: str,
    *,
    settings: Settings | None = None,
) -> PassOutcome:
    settings = settings or get_settings()
    prompt_name = PASS_PROMPTS[pass_label]
    budget = int(settings.ai_max_input_tokens_per_opportunity * _CHARS_PER_TOKEN / 2)
    chunks = build_context(inventory, pass_label, char_budget=budget)
    provider_name, model, warnings = _pass_provider(pass_label, opportunity, settings)
    variables: dict[str, Any] = {
        "DOCUMENT_INVENTORY_JSON": _inventory_json(inventory),
        "SOURCE_CHUNKS": "\n\n".join(c["text"] for c in chunks),
        "AMENDMENT_JSON": _amendment_json(inventory),
    }
    if pass_label == "A":
        variables["OPPORTUNITY_JSON"] = {
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
    manifest = {
        "opportunity_id": opportunity.id,
        "strategy": pass_label,
        "source_snapshots": sorted({d.snapshot_id for d in inventory.documents if d.snapshot_id}),
        "files": [{"file_id": d.file_id, "sha256": d.sha256, "pages": d.page_count} for d in inventory.documents],
        "chunks": [{"chunk_id": c["chunk_id"], "file_id": c["file_id"], "page": c["page"]} for c in chunks],
        "truncated": len(chunks) < len(build_context(inventory, pass_label, char_budget=10**12)),
    }
    if manifest["truncated"]:
        warnings.append({"code": "context_truncated", "severity": "high", "message": f"Pass {pass_label} context exceeded the configured token budget; some source text was not sent."})
    run_type = f"extraction_pass_{pass_label.lower()}"
    try:
        result = run_structured_prompt(
            session,
            opportunity_id=opportunity.id,
            prompt_name=prompt_name,
            analysis_type="compliance_review",
            variables=variables,
            context_manifest=manifest,
            settings=settings,
            provider_name=provider_name,
            model=model,
        )
    except StructuredCallError as exc:
        warnings.append({"code": f"pass_{pass_label.lower()}_{exc.reason}", "severity": "high", "message": f"Extraction pass {pass_label} produced no usable output: {exc.detail}"})
        run = record_run(
            session,
            opportunity_id=opportunity.id,
            run_type=run_type,
            run_version=EXTRACTOR_VERSION,
            output={"error": exc.reason, "detail": exc.detail, "manifest": manifest},
            status="failed",
            warnings=warnings,
            source_snapshot_ids=manifest["source_snapshots"],
        )
        return PassOutcome(pass_label, [], "failed", run.id, warnings=warnings)

    candidates = candidates_from_output(pass_label, result.output.requirements, inventory)
    run = record_run(
        session,
        opportunity_id=opportunity.id,
        run_type=run_type,
        run_version=EXTRACTOR_VERSION,
        output={
            "ai_analysis_id": result.analysis.id,
            "prompt": {"name": result.prompt.name, "version": result.prompt.version, "hash": result.prompt.content_hash},
            "candidates": [c.__dict__ for c in candidates],
            "extraction_notes": result.output.extraction_notes,
        },
        status="incomplete" if manifest["truncated"] else "complete",
        warnings=warnings or None,
        source_snapshot_ids=manifest["source_snapshots"],
        input_hash=result.analysis.input_snapshot_hash,
    )
    return PassOutcome(
        pass_label, candidates, "complete", run.id, result.analysis.id,
        result.analysis.provider, result.analysis.model, warnings,
    )


def run_scanner_pass(session: Session, opportunity_id: int, inventory: Inventory) -> PassOutcome:
    candidates = scan_requirements(inventory)
    run = record_run(
        session,
        opportunity_id=opportunity_id,
        run_type="extraction_pass_d",
        run_version=SCANNER_VERSION,
        output={"candidate_count": len(candidates), "candidates": [c.__dict__ for c in candidates]},
        source_snapshot_ids=sorted({d.snapshot_id for d in inventory.documents if d.snapshot_id}),
    )
    return PassOutcome("D", candidates, "complete", run.id, provider="deterministic")
