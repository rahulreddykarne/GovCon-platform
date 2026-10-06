"""Proposal-to-requirement coverage validation (§15.17).

Checks a selected ``proposal_versions`` row against the matrix. It does not
draft proposals. The deterministic scan requires substantive term overlap in
the section content; a section's heading or its ``requirement_ids`` claim (the
writer model's mapping) is never enough. Term overlap only locates the
passage: a negated or qualified passage ("we do not have ...") or one that
omits a figure the requirement states is routed to NEEDS_REVIEW. Stated
counts ("provide three past-performance references") are counted in the
text. Requirements that call for external forms/attachments are deferred to
submission pre-flight.

The optional ``proposal_coverage`` prompt can downgrade coverage freely. It
can only report COVERED when its excerpt is found verbatim in the proposal
and passes the same content check, and even then the requirement still needs
a verified validation method.
Critical/mandatory NOT_FOUND is a blocking finding.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, TypedDict

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.ai.analysis_types import AnalysisType
from govcon.ai.structured import (
    StructuredCallError,
    checked_output,
    run_structured_prompt,
)
from govcon.compliance.matrix import (
    active_requirements,
    add_evidence,
    close_undetected_findings,
    record_run,
    upsert_open_finding,
)
from govcon.compliance.schemas import ProposalCoverageV1
from govcon.compliance.text import (
    containment,
    numbers,
    quote_in_text,
    split_sentences,
    tokens,
)
from govcon.config import Settings, get_settings
from govcon.models import (
    ProposalSection,
    ProposalVersion,
    Requirement,
    RequirementEvidence,
)
from govcon.security.classification import DataClassification

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
    labels = set(re.findall(rf"\b(?i:{re.escape(noun)})s?\s+(?:#\s*|(?i:no\.?)\s*)?([A-Z]|\d+)\b", text))
    lines = [line for line in text.splitlines() if re.match(rf"\s*(?:[-*•]|\d+[.)])?\s*{re.escape(noun)}\b", line, re.I)]
    return max(len(labels), len(lines))


# Affirmative idioms that contain a negation word ("no later than", "take no
# exception") and the "No. 3" numbering abbreviation.
_AFFIRMATIVE_IDIOMS = re.compile(
    r"\b(?:no|not)\s+(?:later|more|less|fewer|greater)\s+than\b|\bnot\s+to\s+exceed\b|"
    r"\bwithout\s+(?:any\s+)?exceptions?\b|\b(?:takes?|taking|with)\s+no\s+exceptions?\b|\bno\s+exceptions?\s+(?:is|are)\s+taken\b|"
    r"\bno\.?\s*(?=[#\d])",
    re.I,
)
_NEGATION = re.compile(
    r"\b(?:not|no|never|none|nor|neither|cannot|unable|without|lacks?|lacking|decline[sd]?|"
    r"exceptions?\s+to|\w+n['’]t|non-?compliant|noncompliance)\b",
    re.I,
)
_DIGITS = re.compile(r"\b\d+(?:\.\d+)?\b")


def negated(passage: str) -> bool:
    """True when ``passage`` denies or qualifies something (affirmative limit idioms excluded)."""
    return bool(_NEGATION.search(_AFFIRMATIVE_IDIOMS.sub(" ", passage or "")))


def content_issue(text: str, key_values: dict[str, Any], content: str, passages: list[str]) -> str | None:
    """Why matching terms in ``content`` do not establish the obligation, or None.

    Term overlap only locates a candidate passage. COVERED additionally needs
    supporting passages that do not deny or qualify the requirement, and every
    figure the requirement states (quantities, days, dates) restated in the
    content. Anything else is routed to human review.
    """
    if not content.strip():
        return "section has no content"
    if any(negated(p) for p in passages):
        return "supporting passage contains a negation or exception; confirm the proposal commits to the requirement"
    stated = set(_DIGITS.findall(text.replace(",", "")))
    if key_values.get("required_count") and key_values.get("count_noun"):
        stated.discard(str(key_values["required_count"]))
    missing = sorted(stated - numbers(content))
    if missing:
        return f"proposal does not restate required value(s) {', '.join(missing)}"
    return None


class _CoverageLocation(TypedDict):
    section_id: int | None
    section_key: str | None
    excerpt: str | None


def scan_coverage(req_id: Any, text: str, key_values: dict[str, Any], sections: list[SectionView], *, full: float, partial: float) -> CoverageResult:
    """Pure deterministic coverage of one requirement.

    Coverage is scored on section content. A heading only helps locate the
    section: a matching heading over content that does not address the
    requirement is never COVERED.
    """
    wanted = tokens(text) - _GENERIC
    if not wanted:
        return CoverageResult(req_id, "NEEDS_REVIEW", issue="requirement has no distinctive terms to locate")
    scored: list[tuple[float, float, bool, SectionView]] = []
    for section in sections:
        overlap = containment(wanted, tokens(section.content))
        located = containment(wanted, tokens(f"{section.heading or ''} {section.content}"))
        scored.append((overlap, located, req_id in section.requirement_ids, section))
    if not scored:
        return CoverageResult(req_id, "NOT_FOUND", issue="proposal version has no sections")
    scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    overlap, _, mapped, best = scored[0]
    claimed = [s for _, _, m, s in scored if m]
    sentences = split_sentences(best.content) or ([best.content[:300]] if best.content.strip() else [])
    excerpt = max(sentences, key=lambda s: containment(wanted, tokens(s)))[:500] if sentences else None
    base: _CoverageLocation = dict(section_id=best.section_id, section_key=best.section_key, excerpt=excerpt)
    if overlap >= full:
        # Idioms are neutralised before splitting so "No. 3" is not cut into a bare "No.".
        passages = [s for s in split_sentences(_AFFIRMATIVE_IDIOMS.sub(" ", best.content)) if wanted & tokens(s)]
        issue = content_issue(text, key_values, best.content, passages)
        if issue:
            return CoverageResult(req_id, "NEEDS_REVIEW", issue=issue, **base)
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
    headed = max(scored, key=lambda item: (item[1], item[2]))
    if headed[1] >= partial:
        section = headed[3]
        return CoverageResult(
            req_id,
            "NEEDS_REVIEW" if headed[2] else "PARTIAL",
            section_id=section.section_id,
            section_key=section.section_key,
            issue="section heading matches the requirement but its content does not address it",
        )
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
    from govcon.proposals.versions import version_for_opportunity

    settings = settings or get_settings()
    # Ownership first: evidence and findings must never come from another opportunity's proposal.
    _, version = version_for_opportunity(session, opportunity_id, proposal_version_id)
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
                classification=DataClassification.PROPRIETARY,
                opportunity_id=opportunity_id,
                prompt_name="proposal_coverage",
                analysis_type=AnalysisType.PROPOSAL_COVERAGE,
                variables={
                    "REQUIREMENTS_JSON": [{"requirement_id": r.id, "text": r.requirement_text, "type": r.requirement_type, "severity": r.severity, "source_quote": r.source_quote, "deterministic_scan": results[r.id].as_dict()} for r in requirements],
                    "PROPOSAL_TEXT": proposal_text,
                },
                context_manifest={"proposal_version_id": proposal_version_id, "requirement_ids": [r.id for r in requirements]},
                settings=settings,
            )
            for item in checked_output(ai.output, ProposalCoverageV1).coverage:
                current = results.get(item.requirement_id)
                if current is None:
                    continue
                if _ORDER[item.coverage_status] < _ORDER[current.coverage_status]:
                    current.coverage_status = item.coverage_status if item.coverage_status != "COVERED" else current.coverage_status
                    current.issue = f"AI auditor: {item.issue or item.coverage_status}"
                    current.method = "proposal_scan+ai"
                elif item.coverage_status == "COVERED" and current.coverage_status != "COVERED":
                    req = next(r for r in requirements if r.id == item.requirement_id)
                    excerpt_issue = (
                        content_issue(req.requirement_text, req.key_values or {}, item.supporting_excerpt, [item.supporting_excerpt])
                        if item.supporting_excerpt
                        else None
                    )
                    if excerpt_issue:
                        current.issue = f"AI auditor claimed coverage, but its excerpt does not establish it: {excerpt_issue}"
                        current.coverage_status = "NEEDS_REVIEW"
                    elif item.supporting_excerpt and quote_in_text(item.supporting_excerpt, proposal_text):
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
