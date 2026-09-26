"""Proposal-to-requirement coverage validation (§15.17).

Checks a selected ``proposal_versions`` row against the matrix. It does not
draft proposals. The deterministic scan requires substantive term overlap in
the section content; a section's ``requirement_ids`` claim (the writer
model's mapping) is never enough. Stated counts ("provide three
past-performance references") are counted in the text. Requirements that
call for external forms/attachments are deferred to submission pre-flight.

The optional ``proposal_coverage`` prompt can downgrade coverage freely. It
can only report COVERED when its excerpt is found verbatim in the proposal,
and even then the requirement still needs a verified validation method.
Critical/mandatory NOT_FOUND is a blocking finding.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.ai.structured import StructuredCallError, run_structured_prompt
from govcon.compliance.matrix import (
    active_requirements,
    add_evidence,
    close_undetected_findings,
    record_run,
    upsert_open_finding,
)
from govcon.compliance.text import containment, quote_in_text, split_sentences, tokens
from govcon.config import Settings, get_settings
from govcon.models import ProposalSection, ProposalVersion, Requirement, RequirementEvidence

COVERAGE_VERSION = "proposal_coverage.v1"
RESPONSE_TYPES = frozenset({"technical", "past_performance", "pricing", "delivery", "cybersecurity", "country_of_origin", "certification", "representation", "set_aside", "other"})
ARTIFACT_TYPES = frozenset({"signature", "amendment_acknowledgment", "formatting", "page_limit", "submission"})
_GENERIC = frozenset(
    "offeror offerors quoter quoters vendor contractor provide provided submit submitted include included required requirement "
    "proposal proposals quote quotes offer offers response shall must describe demonstrate government solicitation".split()
)
_ORDER = {"NOT_FOUND": 0, "PARTIAL": 1, "NEEDS_REVIEW": 2, "COVERED": 3}


@dataclass
class SectionView:
    section_id: int | None
    section_key: str | None
    heading: str | None
    content: str
    requirement_ids: list[int]


@dataclass
class CoverageResult:
    requirement_id: Any
    coverage_status: str
    section_id: int | None = None
    section_key: str | None = None
    excerpt: str | None = None
    issue: str | None = None
    detected_count: int | None = None
    method: str = "proposal_scan"

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def needs_response(req: Requirement) -> bool:
    if req.requirement_type in ARTIFACT_TYPES:
        return False
    if (req.key_values or {}).get("forms") and req.requirement_type == "administrative":
        return False
    if req.response_required is not None:
        return bool(req.response_required)
    return req.mandatory is not False and (req.requirement_type or "other") in RESPONSE_TYPES


def count_items(noun: str, text: str) -> int:
    labels = set(re.findall(rf"\b{re.escape(noun)}s?\s*(?:#|no\.?)?\s*([A-Z]|\d+)\b", text, re.I))
    lines = [line for line in text.splitlines() if re.match(rf"\s*(?:[-*•]|\d+[.)])?\s*{re.escape(noun)}\b", line, re.I)]
    return max(len(labels), len(lines))


def scan_coverage(req_id: Any, text: str, key_values: dict[str, Any], sections: list[SectionView], *, full: float, partial: float) -> CoverageResult:
    """Pure deterministic coverage of one requirement."""
    wanted = tokens(text) - _GENERIC
    if not wanted:
        return CoverageResult(req_id, "NEEDS_REVIEW", issue="requirement has no distinctive terms to locate")
    scored: list[tuple[float, bool, SectionView]] = []
    for section in sections:
        overlap = containment(wanted, tokens(f"{section.heading or ''} {section.content}"))
        scored.append((overlap, req_id in section.requirement_ids, section))
    if not scored:
        return CoverageResult(req_id, "NOT_FOUND", issue="proposal version has no sections")
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    overlap, mapped, best = scored[0]
    claimed = [s for o, m, s in scored if m]
    excerpt = max(split_sentences(best.content) or [best.content[:300]], key=lambda s: containment(wanted, tokens(s)))[:500]
    base = dict(section_id=best.section_id, section_key=best.section_key, excerpt=excerpt)
    if overlap >= full:
        required = key_values.get("required_count")
        noun = key_values.get("count_noun")
        if required and noun:
            detected = count_items(noun, best.content)
            if detected < int(required):
                return CoverageResult(req_id, "PARTIAL", issue=f"detected {detected} of {required} required {noun}s", detected_count=detected, **base)
            return CoverageResult(req_id, "COVERED", detected_count=detected, **base)
        return CoverageResult(req_id, "COVERED", **base)
    if overlap >= partial:
        return CoverageResult(req_id, "NEEDS_REVIEW" if mapped else "PARTIAL", issue=f"only {overlap:.0%} of requirement terms addressed", **base)
    if claimed:
        section = claimed[0]
        return CoverageResult(req_id, "NEEDS_REVIEW", section_id=section.section_id, section_key=section.section_key, issue="section is mapped to this requirement but its content does not address it")
    return CoverageResult(req_id, "NOT_FOUND", issue="requirement could not be located in the proposal")


def _sections(session: Session, version: ProposalVersion) -> list[SectionView]:
    rows = session.scalars(
        select(ProposalSection).where(ProposalSection.proposal_version_id == version.id).order_by(ProposalSection.sort_order, ProposalSection.id)
    ).all()
    if rows:
        return [SectionView(r.id, r.section_key, r.heading, r.content or "", list(r.requirement_ids or [])) for r in rows]
    return [SectionView(None, "full_text", None, version.full_text or "", [])]


def check_proposal_coverage(
    session: Session,
    opportunity_id: int,
    proposal_version_id: int,
    *,
    use_ai: bool = False,
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    version = session.get(ProposalVersion, proposal_version_id)
    if version is None:
        raise ValueError(f"proposal version not found: {proposal_version_id}")
    sections = _sections(session, version)
    proposal_text = "\n\n".join(f"## [{s.section_key}] {s.heading or ''}\n{s.content}" for s in sections)
    requirements = [r for r in active_requirements(session, opportunity_id) if needs_response(r)]
    deferred = [r.id for r in active_requirements(session, opportunity_id) if not needs_response(r)]
    results = {
        r.id: scan_coverage(r.id, r.requirement_text, r.key_values or {}, sections, full=settings.compliance_coverage_min_overlap, partial=settings.compliance_coverage_partial_overlap)
        for r in requirements
    }
    warnings: list[dict[str, Any]] = []
    ai_summary: dict[str, Any] = {"status": "not_run"}
    if use_ai and requirements:
        try:
            ai = run_structured_prompt(
                session,
                opportunity_id=opportunity_id,
                prompt_name="proposal_coverage",
                analysis_type="proposal_coverage",
                variables={
                    "REQUIREMENTS_JSON": [{"requirement_id": r.id, "text": r.requirement_text, "type": r.requirement_type, "severity": r.severity, "source_quote": r.source_quote, "deterministic_scan": results[r.id].as_dict()} for r in requirements],
                    "PROPOSAL_TEXT": proposal_text,
                },
                context_manifest={"proposal_version_id": proposal_version_id, "requirement_ids": [r.id for r in requirements]},
                settings=settings,
            )
            for item in ai.output.coverage:
                current = results.get(item.requirement_id)
                if current is None:
                    continue
                if _ORDER[item.coverage_status] < _ORDER[current.coverage_status]:
                    current.coverage_status = item.coverage_status if item.coverage_status != "COVERED" else current.coverage_status
                    current.issue = f"AI auditor: {item.issue or item.coverage_status}"
                    current.method = "proposal_scan+ai"
                elif item.coverage_status == "COVERED" and current.coverage_status != "COVERED":
                    if item.supporting_excerpt and quote_in_text(item.supporting_excerpt, proposal_text):
                        current.coverage_status = "COVERED"
                        current.excerpt = item.supporting_excerpt
                        current.method = "ai_excerpt_verified"
                        current.issue = "covered per AI auditor with a verbatim excerpt; needs an independent validation"
                    else:
                        current.issue = "AI auditor claimed coverage without a verifiable excerpt"
                        current.coverage_status = "NEEDS_REVIEW"
            ai_summary = {"status": "complete", "ai_analysis_id": ai.analysis.id}
        except StructuredCallError as exc:
            warnings.append({"code": f"coverage_ai_{exc.reason}", "severity": "medium", "message": exc.detail})
            ai_summary = {"status": "failed", "reason": exc.reason}

    run = record_run(session, opportunity_id=opportunity_id, run_type="proposal_coverage", run_version=COVERAGE_VERSION, output={}, warnings=warnings)
    kept: set[int] = set()
    by_id = {r.id: r for r in requirements}
    existing_evidence = {
        (e.requirement_id, e.proposal_version_id)
        for e in session.scalars(select(RequirementEvidence).where(RequirementEvidence.proposal_version_id == proposal_version_id, RequirementEvidence.evidence_type == "proposal_section")).all()
    }
    for rid, result in results.items():
        req = by_id[rid]
        checks: list[dict[str, Any]] = []
        if result.coverage_status == "COVERED" and (rid, proposal_version_id) not in existing_evidence:
            add_evidence(
                session,
                requirement_id=rid,
                evidence_type="proposal_section",
                verification_method="proposal_scan" if result.method.startswith("proposal_scan") else "ai_validation",
                verification_status="verified",
                description=f"Proposal v{version.version_number} section {result.section_key}",
                proposal_version_id=proposal_version_id,
                source_section=result.section_key,
                source_quote=result.excerpt,
                evidence_value={"detected_count": result.detected_count, "coverage_run_id": run.id},
            )
        elif result.coverage_status == "NOT_FOUND" or (result.coverage_status == "PARTIAL" and result.detected_count is not None):
            checks.append({"validator": "proposal_coverage", "status": "fail", "reason": result.issue, "evidence": {"proposal_version_id": proposal_version_id, "artifact_absent": True, **result.as_dict()}, "validator_version": COVERAGE_VERSION})
        elif result.coverage_status != "COVERED":
            checks.append({"validator": "proposal_coverage", "status": "unknown", "reason": result.issue, "evidence": {"proposal_version_id": proposal_version_id, **result.as_dict()}, "validator_version": COVERAGE_VERSION})
        req.validation = {**(req.validation or {}), "proposal_coverage_checks": checks, "proposal_coverage": {"proposal_version_id": proposal_version_id, **result.as_dict()}}
        if result.coverage_status in {"NOT_FOUND", "PARTIAL"} and (req.mandatory is not False or req.severity == "critical"):
            finding = upsert_open_finding(
                session,
                opportunity_id=opportunity_id,
                requirement_id=rid,
                finding_type="proposal_coverage_gap",
                severity="critical" if req.severity == "critical" else "high",
                description=f"BLOCKING: proposal v{version.version_number} {result.coverage_status}: {req.requirement_text[:200]} ({result.issue})",
                detected_by="proposal_coverage",
                detector_version=COVERAGE_VERSION,
                source_refs={"proposal_version_id": proposal_version_id, **result.as_dict()},
                blocks_submission=True,
                compliance_run_id=run.id,
            )
            kept.add(finding.id)
    close_undetected_findings(session, opportunity_id, detected_by="proposal_coverage", keep_ids=kept, run_id=run.id)
    summary: dict[str, int] = {}
    for result in results.values():
        summary[result.coverage_status] = summary.get(result.coverage_status, 0) + 1
    run.output_json = {
        "proposal_version_id": proposal_version_id,
        "summary": summary,
        "results": {str(k): v.as_dict() for k, v in results.items()},
        "deferred_to_preflight": deferred,
        "ai": ai_summary,
        "blocking_finding_ids": sorted(kept),
    }
    session.flush()
    return {"run_id": run.id, **run.output_json}
