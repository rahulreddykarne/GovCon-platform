"""Deterministic fallback provider for structured decision bundles."""

from __future__ import annotations

from typing import Any

from govcon.decision.provider import ProviderDecision


def _fit_from_score(score: float | None) -> str:
    if score is None:
        return "medium"
    if score >= 0.85:
        return "very_high"
    if score >= 0.7:
        return "high"
    if score >= 0.45:
        return "medium"
    if score >= 0.25:
        return "low"
    return "very_low"


def _risk_from_days(days_remaining: int | float | None) -> str:
    if days_remaining is None:
        return "medium"
    if days_remaining <= 2:
        return "critical"
    if days_remaining <= 5:
        return "high"
    if days_remaining <= 10:
        return "medium"
    return "low"


def _margin_quality(margin_pct: float | None) -> str:
    if margin_pct is None:
        return "marginal"
    if margin_pct < 5:
        return "poor"
    if margin_pct < 12:
        return "marginal"
    if margin_pct < 20:
        return "good"
    return "strong"


def _attractiveness_from_margin(margin_quality: str) -> str:
    return {
        "poor": "very_low",
        "marginal": "low",
        "good": "medium",
        "strong": "high",
    }.get(margin_quality, "medium")


def _missing_core_fields(state: dict[str, Any]) -> list[str]:
    missing = []
    opportunity = state.get("opportunity") or {}
    eligibility = state.get("eligibility") or {}
    sourcing = state.get("sourcing") or {}
    pricing = state.get("pricing") or {}
    if opportunity.get("days_remaining") is None:
        missing.append("opportunity.days_remaining")
    if eligibility.get("set_aside_match") is None:
        missing.append("eligibility.set_aside_match")
    if sourcing.get("product_found") is None:
        missing.append("sourcing.product_found")
    if pricing.get("margin_pct") is None:
        missing.append("pricing.margin_pct")
    return missing


class RuleDecisionProvider:
    """Deterministic fallback provider used when JEV is unavailable."""

    name = "rules"

    def decide(
        self,
        *,
        bundle_name: str,
        bundle_version: str,
        state: dict[str, Any],
    ) -> ProviderDecision:
        handler = getattr(self, f"_bundle_{bundle_name}", None)
        if handler is None:
            raise ValueError(f"unsupported bundle for rules provider: {bundle_name}")
        result, confidence = handler(state)
        return ProviderDecision(
            provider=self.name,
            model="deterministic-v1",
            result=result,
            confidence=confidence,
            latency_ms=0,
        )

    def _bundle_opportunity_triage(self, state: dict[str, Any]) -> tuple[dict[str, Any], float]:
        opportunity = state.get("opportunity") or {}
        capability_score = (state.get("scores") or {}).get("capability_fit_score")
        days_remaining = opportunity.get("days_remaining")
        relevance = "relevant"
        if capability_score is not None and capability_score < 0.25:
            relevance = "not_relevant"
        elif capability_score is None:
            relevance = "review"
        fit = _fit_from_score(capability_score)
        deadline_risk = _risk_from_days(days_remaining)
        priority = "medium"
        if deadline_risk == "critical":
            priority = "critical"
        elif deadline_risk == "high":
            priority = "high"
        human_review = relevance == "review" or deadline_risk in {"high", "critical"}
        next_action = "analyze"
        if relevance == "not_relevant":
            next_action = "dismiss"
        elif human_review:
            next_action = "review"
        return (
            {
                "relevance": relevance,
                "initial_fit": fit,
                "priority": priority,
                "deep_analysis_required": relevance != "not_relevant",
                "attachment_analysis_required": bool((state.get("signals") or {}).get("has_attachments", False)),
                "deadline_risk": deadline_risk,
                "human_review_required": human_review,
                "next_action": next_action,
            },
            0.72 if relevance != "review" else 0.58,
        )

    def _bundle_eligibility_and_execution(
        self, state: dict[str, Any]
    ) -> tuple[dict[str, Any], float]:
        opportunity = state.get("opportunity") or {}
        eligibility = state.get("eligibility") or {}
        sourcing = state.get("sourcing") or {}
        scores = state.get("scores") or {}
        capability = _fit_from_score(scores.get("capability_fit_score"))
        sam_active = eligibility.get("sam_active")
        set_aside_match = eligibility.get("set_aside_match")
        certs_met = eligibility.get("mandatory_certifications_met")
        delivery_feasibility = "review"
        lead_time = sourcing.get("lead_time_days")
        required_days = opportunity.get("required_delivery_days")
        # Feasible only when verified supplier lead time fits the buyer's
        # requirement; either one unknown stays "review".
        if isinstance(lead_time, (int, float)) and isinstance(required_days, (int, float)):
            delivery_feasibility = "yes" if lead_time <= required_days else "no"

        country_risk = "medium"
        if (state.get("compliance") or {}).get("country_of_origin_conflict") is True:
            country_risk = "critical"
        eligibility_clear = sam_active is not False and set_aside_match is not False and certs_met is not False
        stop = sam_active is False or set_aside_match is False or certs_met is False
        execution_risk = _risk_from_days(opportunity.get("days_remaining"))
        if delivery_feasibility == "no":
            execution_risk = "high" if execution_risk != "critical" else execution_risk
        cert_risk = "low"
        if certs_met is False:
            cert_risk = "critical"
        elif certs_met is None:
            cert_risk = "medium"
        return (
            {
                "capability_match": capability,
                "eligibility_clear": bool(eligibility_clear),
                "certification_blocker_risk": cert_risk,
                "delivery_feasibility": delivery_feasibility,
                "country_of_origin_risk": country_risk,
                "execution_risk": execution_risk,
                "stop_evaluation": stop,
                "human_review_required": stop or delivery_feasibility == "review" or certs_met is None,
            },
            0.74 if delivery_feasibility != "review" else 0.6,
        )

    def _bundle_sourcing_and_supplier(
        self, state: dict[str, Any]
    ) -> tuple[dict[str, Any], float]:
        sourcing = state.get("sourcing") or {}
        scores = state.get("scores") or {}
        product_found = sourcing.get("product_found")
        supplier_count = sourcing.get("supplier_count")
        lead_time = sourcing.get("lead_time_days")
        required_days = (state.get("opportunity") or {}).get("required_delivery_days")
        lead_fit = "risky"  # unknown lead time or unknown requirement
        if isinstance(lead_time, (int, float)) and isinstance(required_days, (int, float)):
            lead_fit = "yes" if lead_time <= required_days else "no"

        availability = "review"
        if supplier_count is not None:
            if supplier_count <= 0:
                availability = "no"
            elif supplier_count == 1:
                availability = "review"
            else:
                availability = "yes"
        supplier_risk = "medium" if availability == "review" else ("high" if availability == "no" else "low")
        return (
            {
                "product_match": _fit_from_score(scores.get("product_fit_score")),
                "supplier_risk": supplier_risk,
                "supplier_availability": availability,
                "lead_time_fit": lead_fit,
                "additional_quotes_required": supplier_count is None or supplier_count < 2,
                "preferred_quote_id": None,
                "ready_for_pricing": bool(product_found) and availability == "yes" and lead_fit != "no",
            },
            0.66,
        )

    def _bundle_market_and_pricing(self, state: dict[str, Any]) -> tuple[dict[str, Any], float]:
        pricing = state.get("pricing") or {}
        signals = state.get("signals") or {}
        comparables = int(pricing.get("historical_comparable_count") or 0)
        if comparables >= 8:
            comparability = "high"
        elif comparables >= 3:
            comparability = "medium"
        else:
            comparability = "low"
        competitiveness = "unclear"
        unit_median = pricing.get("historical_median_unit_price")
        unit_price = pricing.get("proposed_unit_price")
        unit_cost = pricing.get("unit_cost")
        if isinstance(unit_median, (int, float)) and isinstance(unit_price, (int, float)):
            # Unit price vs. comparable awards that state a unit price.
            competitiveness = "yes" if unit_price <= unit_median else "no"
        elif isinstance(unit_median, (int, float)) and isinstance(unit_cost, (int, float)):
            competitiveness = "yes" if unit_cost <= unit_median else "no"
        elif "historical_median_unit_price" not in pricing:
            # Externally supplied states (fixtures, JEV callers) may still use the
            # legacy same-unit fields; the engine no longer emits them.
            legacy_median = pricing.get("historical_median")
            supplier_cost = pricing.get("supplier_cost")
            proposed_price = pricing.get("proposed_price")
            if isinstance(supplier_cost, (int, float)) and isinstance(legacy_median, (int, float)):
                competitiveness = "yes" if supplier_cost <= legacy_median else "no"
            elif isinstance(proposed_price, (int, float)) and isinstance(legacy_median, (int, float)):
                competitiveness = "yes" if proposed_price <= legacy_median else "no"
        margin_quality = _margin_quality(pricing.get("margin_pct"))
        pricing_research_required = comparability == "low" or competitiveness in {"no", "unclear"}
        competition_level = "medium"
        if "distinct_awardee_count" in signals:
            awardees = int(signals.get("distinct_awardee_count") or 0)
            if awardees >= 5:
                competition_level = "high"
            elif awardees == 0:
                competition_level = "low"
        else:
            competitor_buckets = int(signals.get("competitor_bucket_count") or 0)
            if competitor_buckets >= 3:
                competition_level = "high"
            elif competitor_buckets == 0:
                competition_level = "low"
        repeat = signals.get("repeat_awardee_signal", signals.get("incumbent_signal"))
        incumbent_advantage = "high" if repeat else "low"
        return (
            {
                "historical_comparability": comparability,
                "supplier_cost_competitiveness": competitiveness,
                "competition_level": competition_level,
                "incumbent_advantage": incumbent_advantage,
                "margin_quality": margin_quality,
                "pricing_research_required": pricing_research_required,
                "commercial_attractiveness": _attractiveness_from_margin(margin_quality),
                "pricing_human_review_required": pricing_research_required or margin_quality in {"poor", "marginal"},
            },
            0.78 if comparability != "low" else 0.55,
        )

    def _bundle_bid_decision(self, state: dict[str, Any]) -> tuple[dict[str, Any], float]:
        bundle_results = state.get("bundle_results") or {}
        market = bundle_results.get("market_and_pricing") or {}
        eligibility = bundle_results.get("eligibility_and_execution") or {}
        opportunity = state.get("opportunity") or {}
        compliance = state.get("compliance") or {}
        pricing = state.get("pricing") or {}

        missing = _missing_core_fields(state)
        days = opportunity.get("days_remaining")
        margin = pricing.get("margin_pct")
        comparables = int(pricing.get("historical_comparable_count") or 0)
        recommendation = "review"
        sufficient_information = not missing
        if missing:
            recommendation = "insufficient_information"
        elif eligibility.get("stop_evaluation"):
            recommendation = "no_bid"
        elif isinstance(margin, (int, float)) and margin < 5:
            recommendation = "no_bid"
        elif isinstance(days, (int, float)) and days <= 1:
            recommendation = "review"
        elif market.get("margin_quality") in {"good", "strong"} and comparables >= 3:
            recommendation = "bid"

        execution_risk = eligibility.get("execution_risk") or _risk_from_days(days)
        compliance_risk = "high" if int(compliance.get("mandatory_missing") or 0) > 0 else "low"
        if int(compliance.get("critical_unresolved") or 0) > 0:
            compliance_risk = "critical"
        confidence = "high" if comparables >= 6 and recommendation == "bid" else "medium"
        if recommendation in {"review", "insufficient_information"}:
            confidence = "low" if recommendation == "insufficient_information" else "medium"
        if recommendation == "no_bid":
            confidence = "high" if not missing else "medium"
        return (
            {
                "recommendation": recommendation,
                "opportunity_strength": eligibility.get("capability_match", "medium"),
                "evidence_confidence": confidence,
                "sufficient_information": sufficient_information,
                "unresolved_no_bid_issue": recommendation == "no_bid",
                "execution_risk": execution_risk,
                "commercial_attractiveness": market.get("commercial_attractiveness", "medium"),
                "compliance_risk": compliance_risk,
                "human_review_required": recommendation != "bid" or confidence != "high",
                "recommend_bid_approval": recommendation == "bid" and confidence == "high",
                "hard_rule_blockers": [],
            },
            {"high": 0.84, "medium": 0.62, "low": 0.38}[confidence],
        )

    def _bundle_compliance_and_amendment(
        self, state: dict[str, Any]
    ) -> tuple[dict[str, Any], float]:
        compliance = state.get("compliance") or {}
        amendment = state.get("amendment") or {}
        mandatory_total = int(compliance.get("mandatory_total") or 0)
        mandatory_unresolved = int(compliance.get("mandatory_missing") or 0)
        critical_total = int(compliance.get("critical_total") or 0)
        critical_unresolved = int(compliance.get("critical_unresolved") or 0)
        need_review = int(compliance.get("needs_review") or 0)
        requirement_decisions = []
        if mandatory_total > 0:
            requirement_decisions.append(
                {
                    "requirement_id": "mandatory-summary",
                    "mandatory": True,
                    "status_assessment": "missing" if mandatory_unresolved else "satisfied",
                    "severity": "high" if mandatory_unresolved else "low",
                    "blocks_submission": mandatory_unresolved > 0,
                    "human_interpretation_required": need_review > 0,
                }
            )
        # Material only when revalidation says so; a count alone is not material.
        if "material_known" in amendment:
            amendment_material = amendment.get("material") is True
        else:
            amendment_material = bool(amendment.get("material")) or int(amendment.get("count") or 0) > 0
        return (
            {
                "requirement_decisions": requirement_decisions,
                "mandatory_total": mandatory_total,
                "mandatory_unresolved": mandatory_unresolved,
                "critical_total": critical_total,
                "critical_unresolved": critical_unresolved,
                "false_satisfied_risk": "high" if critical_unresolved else ("medium" if need_review else "low"),
                "second_validation_required": critical_unresolved > 0 or need_review > 0,
                "amendment_material": amendment_material,
                "proposal_revision_required": amendment_material and (mandatory_unresolved > 0 or critical_unresolved > 0),
                "bid_reassessment_required": mandatory_unresolved > 0 or critical_unresolved > 0,
                "immediate_alert_required": critical_unresolved > 0,
            },
            0.71 if mandatory_total > 0 else 0.55,
        )

    def _bundle_collaborative_review_synthesis(
        self, state: dict[str, Any]
    ) -> tuple[dict[str, Any], float]:
        review = state.get("review") or {}
        bid = (state.get("bundle_results") or {}).get("bid_decision") or {}
        confidence = bid.get("evidence_confidence", "low")
        required = int(review.get("required_review_count") or 1)
        completed = int(review.get("completed_review_count") or 0)
        recommendation = bid.get("recommendation", "review")
        unresolved = list((state.get("analysis") or {}).get("missing_information") or [])
        return (
            {
                "review_policy": review.get("review_policy", "conditional"),
                "required_review_count": required,
                "completed_review_count": completed,
                "quorum_satisfied": completed >= required,
                "second_review_required": confidence == "low" or bool(unresolved),
                "second_review_reason": "low_confidence_or_missing_information" if confidence == "low" or unresolved else None,
                "reviewer_alignment": "single_reviewer" if required <= 1 else "pending",
                "shared_concerns": ["missing_information"] if unresolved else [],
                "material_disagreements": [],
                "new_material_risks": [],
                "unresolved_questions": unresolved,
                "evidence_confidence": confidence,
                "recommendation": recommendation,
                "approval_gate_status": "human_decision_required",
            },
            0.67,
        )

    def _bundle_proposal_review(self, state: dict[str, Any]) -> tuple[dict[str, Any], float]:
        compliance = state.get("compliance") or {}
        mandatory_missing = int(compliance.get("mandatory_missing") or 0)
        quality = "good" if mandatory_missing == 0 else "fair"
        risk = "low" if mandatory_missing == 0 else ("medium" if mandatory_missing < 3 else "high")
        return (
            {
                "ready_for_human_review": True,
                "requirement_coverage": "partial" if mandatory_missing else "yes",
                "unsupported_claims_present": False,
                "quality": quality,
                "compliance_risk": risk,
                "regeneration_required": False,
                "stronger_model_required": False,
                "human_sme_review_required": risk != "low",
                "another_red_team_pass": risk == "high",
            },
            0.64,
        )

    def _bundle_submission_readiness(self, state: dict[str, Any]) -> tuple[dict[str, Any], float]:
        compliance = state.get("compliance") or {}
        blocking = int(compliance.get("critical_unresolved") or 0) > 0 or int(compliance.get("mandatory_missing") or 0) > 0
        status = "not_ready" if blocking else "review"
        return (
            {
                "status": status,
                "blocking_issue_exists": blocking,
                "document_package_complete": "review",
                "override_candidate": "no" if blocking else "review",
                "deadline_critical": _risk_from_days((state.get("opportunity") or {}).get("days_remaining")) == "critical",
                "another_review_required": True,
                "human_verification_required": True,
                "immediate_alert_required": blocking,
            },
            0.63,
        )

    def _bundle_post_submission_routing(self, state: dict[str, Any]) -> tuple[dict[str, Any], float]:
        return (
            {
                "action_required": False,
                "urgency": "low",
                "communication_type": "general",
                "response_required": False,
                "proposal_or_pricing_change_required": False,
                "return_to_review_workflow": False,
            },
            0.6,
        )

    def _bundle_outcome_learning(self, state: dict[str, Any]) -> tuple[dict[str, Any], float]:
        outcome = state.get("outcome") or {}
        evidence_count = int(outcome.get("evidence_count") or 0)
        relevance = "high" if evidence_count >= 3 else ("medium" if evidence_count > 0 else "low")
        return (
            {
                "no_bid_reason": outcome.get("no_bid_reason"),
                "loss_reason": outcome.get("loss_reason"),
                "pricing_factor": "unknown",
                "compliance_factor": "unknown",
                "sourcing_factor": "unknown",
                "deadline_factor": "unknown",
                "use_for_future_analysis": evidence_count > 0,
                "similarity_relevance": relevance,
            },
            0.52,
        )

    def _bundle_model_router(self, state: dict[str, Any]) -> tuple[dict[str, Any], float]:
        signals = state.get("signals") or {}
        deep_needed = bool(signals.get("requires_deep_analysis"))
        high_risk = (state.get("bundle_results") or {}).get("bid_decision", {}).get("execution_risk") in {
            "high",
            "critical",
        }
        return (
            {
                "generative_llm_required": deep_needed,
                "low_cost_model_sufficient": not high_risk,
                "high_capability_model_required": high_risk,
                "second_model_review_required": high_risk,
                "external_ai_policy": "allow",
                "another_model_call_warranted": deep_needed and high_risk,
                "additional_budget_warranted": deep_needed,
            },
            0.59,
        )

    def _bundle_workflow_router(self, state: dict[str, Any]) -> tuple[dict[str, Any], float]:
        bid = (state.get("bundle_results") or {}).get("bid_decision") or {}
        recommendation = bid.get("recommendation")
        if recommendation == "no_bid":
            next_action = "dismiss"
            stage = "no_bid"
        elif recommendation == "bid":
            next_action = "pursue"
            stage = "ready_for_collaborative_review"
        elif recommendation == "insufficient_information":
            next_action = "wait"
            stage = "review"
        else:
            next_action = "review"
            stage = "review"
        return (
            {
                "next_action": next_action,
                "recommended_stage": stage,
                "user_attention_required": recommendation != "bid",
                "safe_to_continue_automatically": recommendation == "bid",
                "sufficient_evidence": recommendation in {"bid", "no_bid"},
                "defer_until_more_information": recommendation == "insufficient_information",
                "rerun_analysis": recommendation == "review",
            },
            0.61,
        )
