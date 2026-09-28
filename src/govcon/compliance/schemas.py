"""Versioned output schemas for the compliance prompts (§40, §39.5).

Every AI output passes JSON parse → these models → semantic checks in the
calling module before anything is persisted. Unknown values stay ``None``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Severity = Literal["critical", "high", "medium", "low"]
ComplianceStatus = Literal["SATISFIED", "MISSING", "UNKNOWN", "NEEDS_REVIEW", "NOT_APPLICABLE", "STALE"]

# Coercive mapping: string confidence labels → float midpoints.
# Models sometimes return "high"/"medium"/"low" instead of a 0-1 float.
_CONFIDENCE_LABEL_MAP: dict[str, float] = {
    "high": 0.85,
    "medium": 0.55,
    "low": 0.20,
    "very_high": 0.95,
    "very_low": 0.10,
}


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

    @field_validator("confidence", mode="before")
    @classmethod
    def coerce_confidence_label(cls, v: object) -> object:
        """Accept string confidence labels ('high', 'medium', 'low') as well as floats."""
        if isinstance(v, str):
            key = v.strip().lower()
            return _CONFIDENCE_LABEL_MAP.get(key)
        return v

    @field_validator("normalized_values", mode="before")
    @classmethod
    def coerce_normalized_values(cls, v: object) -> object:
        """Flatten list-valued entries: take the first scalar or drop the entry."""
        if not isinstance(v, dict):
            return {}
        result: dict[str, str | int | float | bool | None] = {}
        for key, val in v.items():
            if isinstance(val, list):
                scalar = next(
                    (x for x in val if x is None or isinstance(x, (str, int, float, bool))),
                    None,
                )
                result[key] = scalar
            elif isinstance(val, (str, int, float, bool)) or val is None:
                result[key] = val
            else:
                result[key] = str(val)
        return result


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
    quote: str | None = None
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

    @field_validator("unresolved_conflicts", mode="before")
    @classmethod
    def coerce_unresolved_conflicts(cls, v: object) -> object:
        """Coerce list[dict] or other non-list[str] shapes to list[str].

        Observed live shape: list[dict] where each dict describes a conflict.
        Prefer ``description`` / ``text`` / ``topic`` keys; fall back to
        stringifying the whole dict.  Non-list input → empty list.
        """
        if v is None:
            return []
        if isinstance(v, list):
            result: list[str] = []
            for item in v:
                if isinstance(item, str):
                    result.append(item)
                elif isinstance(item, dict):
                    text = (
                        item.get("description")
                        or item.get("text")
                        or item.get("topic")
                        or item.get("summary")
                        or item.get("conflict")
                    )
                    if isinstance(text, str) and text.strip():
                        result.append(text.strip())
                    else:
                        stringified = "; ".join(
                            f"{k}: {val}" for k, val in item.items()
                            if val is not None and not isinstance(val, (dict, list))
                        )
                        if stringified:
                            result.append(stringified)
                elif item is not None:
                    result.append(str(item))
            return result
        return []


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
