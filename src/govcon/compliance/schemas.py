"""Versioned output schemas for the compliance prompts (§40, §39.5).

Every AI output passes JSON parse → these models → semantic checks in the
calling module before anything is persisted. Unknown values stay ``None``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Severity = Literal["critical", "high", "medium", "low"]
ComplianceStatus = Literal["SATISFIED", "MISSING", "UNKNOWN", "NEEDS_REVIEW", "NOT_APPLICABLE", "STALE"]


def _lower_or_none(value):
    if isinstance(value, str):
        cleaned = value.strip().lower()
        return cleaned or None
    return value


class EvidenceRef(BaseModel):
    evidence_id: int | None = None
    evidence_type: str | None = None
    description: str | None = None
    source_file_id: int | None = None
    page: int | None = None
    section: str | None = None
    quote: str | None = None


class ExtractedRequirement(BaseModel):
    requirement_text: str = Field(min_length=3)
    requirement_type: str | None = None
    mandatory: bool | None = None
    severity: Severity | None = None
    response_required: bool | None = None
    source_file_id: int | None = None
    source_page: int | None = None
    source_section: str | None = None
    supporting_quote: str | None = None
    source_snapshot_id: int | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    uncertainty_reason: str | None = None
    normalized_values: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    clause_references: list[str] = Field(default_factory=list)

    _normalize_severity = field_validator("severity", "requirement_type", mode="before")(_lower_or_none)


class RequirementExtractionV1(BaseModel):
    requirements: list[ExtractedRequirement] = Field(default_factory=list)
    extraction_notes: list[str] = Field(default_factory=list)


class ReconciliationGroup(BaseModel):
    candidate_ids: list[str] = Field(min_length=1)
    relationship: Literal["duplicate", "distinct", "conflicting", "possible_supersession", "uncertain"]
    reason: str | None = None


class RequirementReconciliationV1(BaseModel):
    groups: list[ReconciliationGroup] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class RequirementValidation(BaseModel):
    requirement_id: int
    status: ComplianceStatus
    reason: str
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    conflicting_evidence: list[EvidenceRef] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def satisfied_requires_evidence(self):
        if self.status == "SATISFIED" and not self.evidence_refs:
            raise ValueError("SATISFIED requires evidence_refs")
        return self


class ComplianceValidationV1(BaseModel):
    validations: list[RequirementValidation] = Field(default_factory=list)


class ConflictStatement(BaseModel):
    source_file_id: int | None = None
    page: int | None = None
    section: str | None = None
    quote: str
    value: str | None = None
    source_version: str | None = None


class DetectedConflict(BaseModel):
    topic: str
    description: str
    severity: Severity
    statements: list[ConflictStatement] = Field(min_length=2)
    requirement_ids: list[int] = Field(default_factory=list)
    precedence: Literal["resolved_by_version", "ambiguous", "needs_review"] = "needs_review"


class ContradictionDetectionV1(BaseModel):
    conflicts: list[DetectedConflict] = Field(default_factory=list)


class RedTeamFinding(BaseModel):
    finding_type: str
    certainty: Literal["confirmed", "possible"]
    severity: Severity
    description: str
    requirement_id: int | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list)
    missing_evidence: str | None = None

    @model_validator(mode="after")
    def needs_evidence_or_gap(self):
        if not self.evidence and not (self.missing_evidence and self.missing_evidence.strip()):
            raise ValueError("finding must include evidence or explain what evidence is missing")
        return self


class ComplianceRedTeamV1(BaseModel):
    findings: list[RedTeamFinding] = Field(default_factory=list)


class CoverageItem(BaseModel):
    requirement_id: int
    coverage_status: Literal["COVERED", "PARTIAL", "NOT_FOUND", "NEEDS_REVIEW"]
    proposal_section: str | None = None
    proposal_page: int | None = None
    supporting_excerpt: str | None = None
    source_requirement_ref: str | None = None
    issue: str | None = None


class ProposalCoverageV1(BaseModel):
    coverage: list[CoverageItem] = Field(default_factory=list)


class PreflightIssue(BaseModel):
    issue_type: str
    severity: Severity
    description: str
    evidence: list[EvidenceRef] = Field(default_factory=list)


class SubmissionPreflightAIV1(BaseModel):
    status: Literal["READY", "NOT_READY", "NEEDS_REVIEW"]
    issues: list[PreflightIssue] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)


class AmendmentChange(BaseModel):
    change_type: str
    old_state: str | None = None
    new_state: str | None = None
    source_evidence: list[EvidenceRef] = Field(default_factory=list)
    affected_requirement_ids: list[int] = Field(default_factory=list)
    affected_proposal_sections: list[str] = Field(default_factory=list)
    pricing_impact: bool | None = None
    sourcing_impact: bool | None = None
    compliance_impact: bool | None = None


class AmendmentAnalysisV1(BaseModel):
    material: bool | None = None
    changes: list[AmendmentChange] = Field(default_factory=list)
    unresolved_conflicts: list[str] = Field(default_factory=list)


class ProposalDraftSection(BaseModel):
    section_key: str
    heading: str | None = None
    content: str
    requirement_ids: list[int] = Field(default_factory=list)
    source_fact_ids: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    word_count: int | None = None


class ProposalDraftV1(BaseModel):
    sections: list[ProposalDraftSection] = Field(default_factory=list)
    global_blockers: list[str] = Field(default_factory=list)
    source_fact_ids_used: list[str] = Field(default_factory=list)
    draft_notes: list[str] = Field(default_factory=list)


ProposalSeverity = Literal["critical", "major", "minor"]


class ProposalRedTeamFinding(BaseModel):
    finding_id: str | None = None
    severity: ProposalSeverity
    category: str | None = None
    requirement_id: int | None = None
    proposal_section: str | None = None
    description: str
    evidence: str | None = None
    recommended_fix: str | None = None


class ProposalRedTeamV1(BaseModel):
    findings: list[ProposalRedTeamFinding] = Field(default_factory=list)
    critical_count: int = 0
    major_count: int = 0
    minor_count: int = 0
    overall_assessment: Literal["READY", "NOT_READY", "NEEDS_REVIEW"] = "NEEDS_REVIEW"
    reviewer_notes: list[str] = Field(default_factory=list)


COMPLIANCE_SCHEMAS: dict[str, type[BaseModel]] = {
    "requirement_extraction.v1": RequirementExtractionV1,
    "requirement_reconciliation.v1": RequirementReconciliationV1,
    "compliance_validation.v1": ComplianceValidationV1,
    "contradiction_detection.v1": ContradictionDetectionV1,
    "compliance_red_team.v1": ComplianceRedTeamV1,
    "proposal_coverage.v1": ProposalCoverageV1,
    "submission_preflight_ai.v1": SubmissionPreflightAIV1,
    "amendment_analysis.v1": AmendmentAnalysisV1,
    "proposal_draft.v1": ProposalDraftV1,
    "proposal_red_team.v1": ProposalRedTeamV1,
}
