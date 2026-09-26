"""Pydantic schemas for Phase 8 decision bundles."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


ConfidenceLevel = Literal["low", "medium", "high"]
RiskLevel = Literal["low", "medium", "high", "critical"]
FitLevel = Literal["very_low", "low", "medium", "high", "very_high"]
AttractivenessLevel = Literal["very_low", "low", "medium", "high", "very_high"]
NextAction = Literal["dismiss", "analyze", "review", "pursue", "wait"]


class OpportunityTriageResult(BaseModel):
    relevance: Literal["relevant", "not_relevant", "review"]
    initial_fit: FitLevel
    priority: Literal["low", "medium", "high", "critical"]
    deep_analysis_required: bool
    attachment_analysis_required: bool
    deadline_risk: RiskLevel
    human_review_required: bool
    next_action: NextAction


class EligibilityExecutionResult(BaseModel):
    capability_match: FitLevel
    eligibility_clear: bool
    certification_blocker_risk: RiskLevel
    delivery_feasibility: Literal["yes", "no", "review"]
    country_of_origin_risk: RiskLevel
    execution_risk: RiskLevel
    stop_evaluation: bool
    human_review_required: bool


class SourcingSupplierResult(BaseModel):
    product_match: FitLevel
    supplier_risk: Literal["low", "medium", "high"]
    supplier_availability: Literal["yes", "no", "review"]
    lead_time_fit: Literal["yes", "no", "risky"]
    additional_quotes_required: bool
    preferred_quote_id: int | None = None
    ready_for_pricing: bool


class MarketPricingResult(BaseModel):
    historical_comparability: Literal["low", "medium", "high"]
    supplier_cost_competitiveness: Literal["yes", "no", "unclear"]
    competition_level: Literal["low", "medium", "high"]
    incumbent_advantage: Literal["low", "medium", "high"]
    margin_quality: Literal["poor", "marginal", "good", "strong"]
    pricing_research_required: bool
    commercial_attractiveness: AttractivenessLevel
    pricing_human_review_required: bool


class BidDecisionResult(BaseModel):
    recommendation: Literal["bid", "no_bid", "review", "insufficient_information"]
    opportunity_strength: FitLevel
    evidence_confidence: ConfidenceLevel
    sufficient_information: bool
    unresolved_no_bid_issue: bool
    execution_risk: RiskLevel
    commercial_attractiveness: AttractivenessLevel
    compliance_risk: RiskLevel
    human_review_required: bool
    recommend_bid_approval: bool
    hard_rule_blockers: list[str] = Field(default_factory=list)


class RequirementDecision(BaseModel):
    requirement_id: int | str
    mandatory: bool
    status_assessment: Literal["satisfied", "missing", "review"]
    severity: RiskLevel
    blocks_submission: bool
    human_interpretation_required: bool


class ComplianceAmendmentResult(BaseModel):
    requirement_decisions: list[RequirementDecision] = Field(default_factory=list)
    mandatory_total: int
    mandatory_unresolved: int
    critical_total: int
    critical_unresolved: int
    false_satisfied_risk: Literal["low", "medium", "high"]
    second_validation_required: bool
    amendment_material: bool
    proposal_revision_required: bool
    bid_reassessment_required: bool
    immediate_alert_required: bool


class CollaborativeReviewSynthesisResult(BaseModel):
    review_policy: Literal["single", "dual", "conditional"]
    required_review_count: int
    completed_review_count: int
    quorum_satisfied: bool
    second_review_required: bool
    second_review_reason: str | None = None
    reviewer_alignment: str
    shared_concerns: list[str] = Field(default_factory=list)
    material_disagreements: list[str] = Field(default_factory=list)
    new_material_risks: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    evidence_confidence: ConfidenceLevel
    recommendation: Literal["bid", "no_bid", "review", "insufficient_information"]
    approval_gate_status: Literal["human_decision_required", "needs_more_review", "ready"]


class ProposalReviewResult(BaseModel):
    ready_for_human_review: bool
    requirement_coverage: Literal["yes", "no", "partial"]
    unsupported_claims_present: bool
    quality: Literal["poor", "fair", "good", "strong"]
    compliance_risk: Literal["low", "medium", "high"]
    regeneration_required: bool
    stronger_model_required: bool
    human_sme_review_required: bool
    another_red_team_pass: bool


class SubmissionReadinessResult(BaseModel):
    status: Literal["ready", "not_ready", "review"]
    blocking_issue_exists: bool
    document_package_complete: Literal["yes", "no", "review"]
    override_candidate: Literal["yes", "no", "review"]
    deadline_critical: bool
    another_review_required: bool
    human_verification_required: bool
    immediate_alert_required: bool


class PostSubmissionRoutingResult(BaseModel):
    action_required: bool
    urgency: Literal["low", "medium", "high", "critical"]
    communication_type: Literal[
        "amendment",
        "clarification",
        "award_notice",
        "rejection",
        "request_for_information",
        "general",
        "review",
    ]
    response_required: bool
    proposal_or_pricing_change_required: bool
    return_to_review_workflow: bool


class OutcomeLearningResult(BaseModel):
    no_bid_reason: str | None = None
    loss_reason: str | None = None
    pricing_factor: Literal["yes", "no", "unknown"]
    compliance_factor: Literal["yes", "no", "unknown"]
    sourcing_factor: Literal["yes", "no", "unknown"]
    deadline_factor: Literal["yes", "no", "unknown"]
    use_for_future_analysis: bool
    similarity_relevance: Literal["low", "medium", "high"]


class ModelRouterResult(BaseModel):
    generative_llm_required: bool
    low_cost_model_sufficient: bool
    high_capability_model_required: bool
    second_model_review_required: bool
    external_ai_policy: Literal["allow", "block", "human_review"]
    another_model_call_warranted: bool
    additional_budget_warranted: bool


class WorkflowRouterResult(BaseModel):
    next_action: NextAction
    recommended_stage: Literal[
        "evaluating",
        "review",
        "ready_for_collaborative_review",
        "ready_to_submit",
        "submitted",
        "no_bid",
    ]
    user_attention_required: bool
    safe_to_continue_automatically: bool
    sufficient_evidence: bool
    defer_until_more_information: bool
    rerun_analysis: bool


class PreliminaryRecommendation(BaseModel):
    recommendation: Literal["bid", "no_bid", "review", "insufficient_information"]
    score: float = Field(ge=0.0, le=1.0)
    strengths: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    factor_scores: dict[str, float | None] = Field(default_factory=dict)
    human_review_required: bool = True


BUNDLE_SCHEMAS: dict[str, type[BaseModel]] = {
    "opportunity_triage": OpportunityTriageResult,
    "eligibility_and_execution": EligibilityExecutionResult,
    "sourcing_and_supplier": SourcingSupplierResult,
    "market_and_pricing": MarketPricingResult,
    "bid_decision": BidDecisionResult,
    "compliance_and_amendment": ComplianceAmendmentResult,
    "collaborative_review_synthesis": CollaborativeReviewSynthesisResult,
    "proposal_review": ProposalReviewResult,
    "submission_readiness": SubmissionReadinessResult,
    "post_submission_routing": PostSubmissionRoutingResult,
    "outcome_learning": OutcomeLearningResult,
    "model_router": ModelRouterResult,
    "workflow_router": WorkflowRouterResult,
}


ACCEPTANCE_BUNDLE_NAMES: tuple[str, ...] = (
    "opportunity_triage",
    "eligibility_and_execution",
    "market_and_pricing",
    "bid_decision",
    "compliance_and_amendment",
    "collaborative_review_synthesis",
    "proposal_review",
    "outcome_learning",
)


def validate_bundle_result(bundle_name: str, data: dict[str, Any]) -> BaseModel:
    schema = BUNDLE_SCHEMAS.get(bundle_name)
    if schema is None:
        raise ValueError(f"unknown decision bundle: {bundle_name}")
    return schema.model_validate(data)
