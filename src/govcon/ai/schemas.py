"""Pydantic models for AI analysis output validation.

Every structured AI analysis output must be validated through these schemas
before persistence. Malformed output fails closed — it is never silently
stored as valid.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SourceRef(BaseModel):
    source_file_id: int | None = None
    page: int | None = None
    section: str | None = None
    quote: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _label_only(cls, value: object) -> object:
        # Models cite non-file inputs (e.g. "AWARDS_JSON") as a bare string; keep
        # it as the section label. It names no file, so it never counts as a
        # verified citation.
        if isinstance(value, str):
            return {"section": value}
        if isinstance(value, dict) and isinstance(value.get("quote"), str) and len(value["quote"]) > 200:
            return {**value, "quote": value["quote"][:200]}
        return value


class LineItem(BaseModel):
    description: str | None = None
    manufacturer: str | None = None
    part_number: str | None = None
    nsn: str | None = None
    quantity: int | float | None = None
    unit: str | None = None
    source_refs: list[SourceRef] = Field(default_factory=list)


class DeliveryInfo(BaseModel):
    location: str | None = None
    required_date: str | None = None
    delivery_days: int | None = None
    source_refs: list[SourceRef] = Field(default_factory=list)


class KeyDate(BaseModel):
    label: str
    date: str | None = None
    timezone: str | None = None
    source_refs: list[SourceRef] = Field(default_factory=list)


class SubmissionInfo(BaseModel):
    method: str | None = None
    portal: str | None = None
    recipient_email: str | None = None
    deadline: str | None = None
    timezone: str | None = None
    required_files: list[str] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(default_factory=list)


class EligibilityInfo(BaseModel):
    set_aside: str | None = None
    small_business_size_standard: str | None = None
    other_restrictions: list[str] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(default_factory=list)


class EvaluationFactor(BaseModel):
    name: str
    weight: str | None = None
    description: str | None = None
    source_refs: list[SourceRef] = Field(default_factory=list)


class PricingStructure(BaseModel):
    format: str | None = None
    instructions: str | None = None
    source_refs: list[SourceRef] = Field(default_factory=list)


class AmendmentStatus(BaseModel):
    amendment_count: int | None = None
    latest_amendment: str | None = None
    summary: str | None = None
    source_refs: list[SourceRef] = Field(default_factory=list)


class RiskFlag(BaseModel):
    category: str
    description: str
    severity: str | None = None
    source_refs: list[SourceRef] = Field(default_factory=list)


class MissingInfo(BaseModel):
    field: str
    reason: str
    impact: str | None = None


class SolicitationAnalysisV1(BaseModel):
    """Schema for solicitation_analysis.v1 output."""

    model_config = ConfigDict(extra="forbid")

    summary: str | None = None
    items: list[LineItem] = Field(default_factory=list)
    key_dates: list[KeyDate] = Field(default_factory=list)
    delivery: DeliveryInfo | None = None
    eligibility: EligibilityInfo | None = None
    evaluation_factors: list[EvaluationFactor] = Field(default_factory=list)
    past_performance_requirements: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    country_of_origin_references: list[str] = Field(default_factory=list)
    submission: SubmissionInfo | None = None
    pricing_structure: PricingStructure | None = None
    amendment_status: AmendmentStatus | None = None
    risk_flags: list[RiskFlag] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    missing_information: list[MissingInfo] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(default_factory=list)
    clauses: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _requires_content(self) -> SolicitationAnalysisV1:
        if not _has_analysis_content(self.model_dump(mode="json")):
            raise ValueError("solicitation analysis must contain useful facts or explicit missing information")
        return self


def _has_analysis_content(value: object) -> bool:
    if isinstance(value, dict):
        return any(_has_analysis_content(item) for key, item in value.items() if key != "source_refs")
    if isinstance(value, list):
        return any(_has_analysis_content(item) for item in value)
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None


class ReviewEvidenceItem(BaseModel):
    source_file_id: int | None = None
    page: int | None = None
    section: str | None = None
    quote: str | None = None
    summary: str | None = None


class ReviewerCommentValidationV1(BaseModel):
    position: Literal[
        "agree",
        "partially_agree",
        "disagree",
        "insufficient_evidence",
        "needs_human_review",
    ]
    confidence: Literal["low", "medium", "high"]
    reason: str = Field(min_length=1)
    supporting_evidence: list[ReviewEvidenceItem] = Field(default_factory=list)
    contradicting_evidence: list[ReviewEvidenceItem] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    suggested_action: str | None = None


class ConsolidatedReviewV1(BaseModel):
    reviewer_alignment: str
    review_mode: Literal["single", "dual", "conditional", "override-driven"] | None = None
    shared_concerns: list[str] = Field(default_factory=list)
    disagreements: list[str] = Field(default_factory=list)
    disagreements_with_ai_package: list[str] = Field(default_factory=list)
    new_material_risks: list[str] = Field(default_factory=list)
    resolved_issues: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    evidence_needed_before_approval: list[str] = Field(default_factory=list)
    prior_analysis_stale: bool | None = None
    jev_final_recommendation: Literal["bid", "no_bid", "review", "insufficient_information"] | None = None
    human_approval_required: bool = True
    summary: str | None = None


class OutcomeAnalysisV1(BaseModel):
    """Schema for outcome_analysis.v1 — evidence-constrained outcome classification."""

    no_bid_reason: str | None = None
    loss_reason: str | None = None
    win_reason: str | None = None
    pricing_factor: Literal["yes", "no", "UNKNOWN"] = "UNKNOWN"
    compliance_factor: Literal["yes", "no", "UNKNOWN"] = "UNKNOWN"
    sourcing_factor: Literal["yes", "no", "UNKNOWN"] = "UNKNOWN"
    deadline_factor: Literal["yes", "no", "UNKNOWN"] = "UNKNOWN"
    eligibility_factor: Literal["yes", "no", "UNKNOWN"] = "UNKNOWN"
    delivery_factor: Literal["yes", "no", "UNKNOWN"] = "UNKNOWN"
    competition_factor: Literal["yes", "no", "UNKNOWN"] = "UNKNOWN"
    administrative_factor: Literal["yes", "no", "UNKNOWN"] = "UNKNOWN"
    strategic_no_bid: Literal["yes", "no", "UNKNOWN"] = "UNKNOWN"
    direct_feedback_present: bool = False
    use_for_future_analysis: bool = True
    confidence: Literal["high", "medium", "low"] = "low"
    evidence_summary: str | None = None


# ---------------------------------------------------------------------------
# §39.2 market_analysis.v1
# ---------------------------------------------------------------------------

class ComparableAward(BaseModel):
    vendor: str | None = None
    amount: float | None = None
    date: str | None = None
    nsn: str | None = None
    psc: str | None = None
    comparability_note: str | None = None
    source_refs: list[SourceRef] = Field(default_factory=list)


class MarketAnalysisV1(BaseModel):
    """Schema for market_analysis.v1 — government-contract market intelligence."""

    comparable_awards: list[ComparableAward] = Field(default_factory=list)
    historical_winners: list[str] = Field(default_factory=list)
    incumbent_signals: list[str] = Field(default_factory=list)
    recurring_vendors: list[str] = Field(default_factory=list)
    price_comparability: str | None = None
    agency_buying_patterns: str | None = None
    competition_signals: list[str] = Field(default_factory=list)
    recompete_signals: list[str] = Field(default_factory=list)
    comparability_weaknesses: list[str] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# §39.3 supplier_analysis.v1
# ---------------------------------------------------------------------------

class SupplierCandidate(BaseModel):
    supplier: str | None = None
    product: str | None = None
    exact_requirement_matches: list[str] = Field(default_factory=list)
    partial_requirement_matches: list[str] = Field(default_factory=list)
    unsupported_claims: list[str] = Field(default_factory=list)
    specification_mismatches: list[str] = Field(default_factory=list)
    delivery_lead_time_risk: str | None = None
    origin_compliance_gaps: list[str] = Field(default_factory=list)
    quote_commercial_risks: list[str] = Field(default_factory=list)
    evidence_still_required: list[str] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(default_factory=list)


class SupplierAnalysisV1(BaseModel):
    """Schema for supplier_analysis.v1 — sourcing evidence analysis."""

    candidates: list[SupplierCandidate] = Field(default_factory=list)
    overall_sourcing_risk: Literal["low", "medium", "high", "UNKNOWN"] = "UNKNOWN"
    recommended_next_steps: list[str] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# supplier_quote_extraction.v1 (ADR-071)
# ---------------------------------------------------------------------------

class QuoteLineV1(BaseModel):
    description: str | None = None
    part_number: str | None = None
    nsn: str | None = None
    quantity: float | None = None
    unit: str | None = None
    unit_price: float | None = None
    extended_price: float | None = None
    lead_time_days: int | None = None
    source_quote: str | None = None


class SupplierQuoteExtractionV1(BaseModel):
    """Schema for supplier_quote_extraction.v1 — lines read from a supplier's quote document."""

    supplier_name: str | None = None
    quote_number: str | None = None
    valid_until: str | None = None
    currency: str | None = None
    total_price: float | None = None
    lines: list[QuoteLineV1] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# §39.4 pricing_analysis.v1
# ---------------------------------------------------------------------------

class PricingAnalysisV1(BaseModel):
    """Schema for pricing_analysis.v1 — bid-pricing commercial analysis."""

    historical_comparability: str | None = None
    proposed_price_position: str | None = None
    expected_margin_quality: str | None = None
    cost_risk_signals: list[str] = Field(default_factory=list)
    missing_cost_inputs: list[str] = Field(default_factory=list)
    pricing_evidence_gaps: list[str] = Field(default_factory=list)
    more_research_warranted: bool = False
    confidence: Literal["high", "medium", "low"] = "low"
    source_refs: list[SourceRef] = Field(default_factory=list)


SCHEMA_REGISTRY: dict[str, type[BaseModel]] = {
    "solicitation_analysis.v1": SolicitationAnalysisV1,
    "supplier_quote_extraction.v1": SupplierQuoteExtractionV1,
    "reviewer_comment_validation.v1": ReviewerCommentValidationV1,
    "consolidated_review.v1": ConsolidatedReviewV1,
    "outcome_analysis.v1": OutcomeAnalysisV1,
    "market_analysis.v1": MarketAnalysisV1,
    "supplier_analysis.v1": SupplierAnalysisV1,
    "pricing_analysis.v1": PricingAnalysisV1,
}

from govcon.compliance.schemas import COMPLIANCE_SCHEMAS

SCHEMA_REGISTRY.update(COMPLIANCE_SCHEMAS)


def validate_analysis_output(schema_version: str, data: dict) -> BaseModel:
    """Validate AI output against its declared schema version.

    Raises ``ValueError`` when the schema version is unknown.
    Raises ``pydantic.ValidationError`` when the data does not conform.
    """
    schema_cls = SCHEMA_REGISTRY.get(schema_version)
    if schema_cls is None:
        raise ValueError(f"unknown analysis schema version: {schema_version}")
    return schema_cls.model_validate(data)
