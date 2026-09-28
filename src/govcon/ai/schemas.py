"""Pydantic models for AI analysis output validation.

Every structured AI analysis output must be validated through these schemas
before persistence. Malformed output fails closed — it is never silently
stored as valid.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class SourceRef(BaseModel):
    source_file_id: int | None = None
    page: int | None = None
    section: str | None = None
    quote: str | None = None


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

    @model_validator(mode="before")
    @classmethod
    def coerce_from_string(cls, v: object) -> object:
        """Accept a bare string as the factor name."""
        if isinstance(v, str):
            return {"name": v}
        return v


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

    @model_validator(mode="before")
    @classmethod
    def coerce_from_string(cls, v: object) -> object:
        """Accept a bare string or non-standard dict keys as field/reason.

        Handles observed live shapes:
        - bare string → {"field": v, "reason": v}
        - {"item": ..., "status": ...} → {"field": item, "reason": status}
        - {"description": ...} or other single-key dicts → field=value, reason=value
        """
        if isinstance(v, str):
            return {"field": v, "reason": v}
        if isinstance(v, dict):
            if "field" not in v and "item" in v:
                item_val = str(v["item"]) if v.get("item") is not None else ""
                status_val = str(v.get("status", item_val)) if v.get("status") is not None else item_val
                remapped: dict[str, object] = {"field": item_val, "reason": status_val}
                if "impact" in v:
                    remapped["impact"] = v["impact"]
                return remapped
        return v


class SolicitationAnalysisV1(BaseModel):
    """Schema for solicitation_analysis.v1 output."""

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

    @field_validator("evaluation_factors", mode="before")
    @classmethod
    def coerce_evaluation_factors(cls, v: object) -> object:
        """Accept a dict of {name: weight/description} as a list of factors."""
        if isinstance(v, dict):
            return [
                {"name": k, "description": str(val) if val is not None else None}
                for k, val in v.items()
            ]
        if not isinstance(v, list):
            return []
        return v

    @field_validator("past_performance_requirements", mode="before")
    @classmethod
    def coerce_past_performance_requirements(cls, v: object) -> object:
        """Coerce dict or list[dict] to list[str]; no facts are invented.

        Observed live shapes:
        - dict {key: value, ...} → extract string values (non-empty)
        - list[dict] → stringify each dict using its most descriptive value
        """
        if isinstance(v, dict):
            parts: list[str] = []
            for val in v.values():
                if isinstance(val, str) and val.strip():
                    parts.append(val.strip())
                elif val is not None and not isinstance(val, (dict, list)):
                    parts.append(str(val))
            return parts
        if isinstance(v, list):
            result: list[str] = []
            for item in v:
                if isinstance(item, str):
                    result.append(item)
                elif isinstance(item, dict):
                    text = (
                        item.get("description")
                        or item.get("requirement")
                        or item.get("text")
                        or item.get("value")
                        or item.get("name")
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
        if v is None:
            return []
        return v

    @field_validator("country_of_origin_references", mode="before")
    @classmethod
    def coerce_country_of_origin_references(cls, v: object) -> object:
        """Coerce reference objects and maps to list[str] without dropping values.

        Observed live shape: list[dict] where each dict has keys such as
        ``clause``, ``reference``, ``description``, ``text``.
        """
        if isinstance(v, dict):
            preferred = ("clause", "reference", "text", "description", "name", "value")
            if any(key in v for key in preferred):
                v = [v]
            else:
                return [
                    f"{key}: {value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)}"
                    for key, value in v.items()
                    if value is not None and (not isinstance(value, str) or value.strip())
                ]
        if isinstance(v, list):
            result: list[str] = []
            for item in v:
                if isinstance(item, str):
                    result.append(item)
                elif isinstance(item, dict):
                    text = (
                        item.get("clause")
                        or item.get("reference")
                        or item.get("text")
                        or item.get("description")
                        or item.get("name")
                        or item.get("value")
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
        if v is None:
            return []
        return v

    @field_validator("missing_information", mode="before")
    @classmethod
    def coerce_missing_information(cls, v: object) -> object:
        """Ensure the field is always a list; individual items coerced by MissingInfo."""
        if not isinstance(v, list):
            return []
        return v


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
    "reviewer_comment_validation.v1": ReviewerCommentValidationV1,
    "consolidated_review.v1": ConsolidatedReviewV1,
    "outcome_analysis.v1": OutcomeAnalysisV1,
    "market_analysis.v1": MarketAnalysisV1,
    "supplier_analysis.v1": SupplierAnalysisV1,
    "pricing_analysis.v1": PricingAnalysisV1,
}

from govcon.compliance.schemas import COMPLIANCE_SCHEMAS  # noqa: E402

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
