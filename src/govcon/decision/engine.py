"""Phase 8 decision orchestration and persistence."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from statistics import median
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from govcon.config import Settings, get_settings
from govcon.decision.bundles import bundle_definition, load_decision_spec_metadata

logger = logging.getLogger("govcon.decision.engine")
from govcon.ai.gateway import AIGatewayBlocked
from govcon.decision.provider import (
    DecisionProviderUnavailable,
    ProviderDecision,
)
from govcon.decision.providers import JevDecisionProvider, LLMDecisionProvider, RuleDecisionProvider
from govcon.decision.rules import (
    apply_hard_rule_override,
    confidence_to_score,
    enforce_low_confidence_escalation,
    evaluate_hard_rules,
)
from govcon.decision.schemas import PreliminaryRecommendation, validate_bundle_result
from govcon.ingest.snapshots import canonical_content_hash, json_safe
from govcon.intelligence.competitors import competitor_summary
from govcon.matching.pricing import recent_award_comps
from govcon.models import (
    AIAnalysis,
    BidDecision,
    DecisionRun,
    Opportunity,
    OpportunitySnapshot,
    Pursuit,
    Requirement,
    ReviewSession,
)


@dataclass(frozen=True)
class BundleExecution:
    bundle_name: str
    run: DecisionRun
    result: dict[str, Any]
    provider: str
    model: str | None
    confidence: float | None
    hard_rule_findings: tuple[str, ...]


@dataclass(frozen=True)
class DecisionPackageExecution:
    analysis: AIAnalysis
    bid_decision: BidDecision
    bundle_runs: tuple[BundleExecution, ...]
    package_output: dict[str, Any]


def build_decision_state(session: Session, opportunity_id: int) -> dict[str, Any]:
    """Build source-backed state for decision bundles."""
    opportunity = session.get(Opportunity, opportunity_id)
    if opportunity is None:
        raise ValueError(f"opportunity not found: {opportunity_id}")

    now = datetime.now(UTC)
    days_remaining = None
    if opportunity.response_deadline is not None:
        delta = opportunity.response_deadline - now
        days_remaining = int(delta.total_seconds() // 86400)

    latest_summary = session.scalar(
        select(AIAnalysis)
        .where(
            AIAnalysis.opportunity_id == opportunity_id,
            AIAnalysis.analysis_type == "solicitation_summary",
        )
        .order_by(desc(AIAnalysis.created_at), desc(AIAnalysis.id))
        .limit(1)
    )
    summary_json = latest_summary.output_json if latest_summary and isinstance(latest_summary.output_json, dict) else {}

    latest_pursuit = session.scalar(select(Pursuit).where(Pursuit.opportunity_id == opportunity_id).limit(1))
    latest_review = session.scalar(select(ReviewSession).where(ReviewSession.opportunity_id == opportunity_id).limit(1))

    source_snapshot_ids = [
        row[0]
        for row in session.execute(
            select(OpportunitySnapshot.id)
            .where(OpportunitySnapshot.opportunity_id == opportunity_id)
            .order_by(desc(OpportunitySnapshot.fetched_at), desc(OpportunitySnapshot.id))
            .limit(5)
        ).all()
    ]

    award_comps = recent_award_comps(session, nsn=opportunity.nsn, psc_code=opportunity.psc_code, limit=10)
    comparable_amounts = [float(point.amount) for point in award_comps if point.amount is not None]
    historical_median = float(median(comparable_amounts)) if comparable_amounts else None

    comp_summary = competitor_summary(session, opportunity_id, limit=5)
    competitor_bucket_count = len(comp_summary.buckets) if comp_summary else 0

    mandatory_total = int(
        session.scalar(
            select(func.count())
            .select_from(Requirement)
            .where(Requirement.opportunity_id == opportunity_id, Requirement.mandatory.is_(True))
        )
        or 0
    )
    mandatory_missing = int(
        session.scalar(
            select(func.count())
            .select_from(Requirement)
            .where(
                Requirement.opportunity_id == opportunity_id,
                Requirement.mandatory.is_(True),
                Requirement.status.in_(["missing", "needs_review", "unknown", "unreviewed", "stale"]),
            )
        )
        or 0
    )
    needs_review = int(
        session.scalar(
            select(func.count())
            .select_from(Requirement)
            .where(
                Requirement.opportunity_id == opportunity_id,
                Requirement.status.in_(["needs_review", "unknown", "unreviewed"]),
            )
        )
        or 0
    )
    critical_total = int(
        session.scalar(
            select(func.count())
            .select_from(Requirement)
            .where(Requirement.opportunity_id == opportunity_id, Requirement.severity == "critical")
        )
        or 0
    )
    critical_unresolved = int(
        session.scalar(
            select(func.count())
            .select_from(Requirement)
            .where(
                Requirement.opportunity_id == opportunity_id,
                Requirement.severity == "critical",
                Requirement.status.in_(["missing", "needs_review", "unknown", "unreviewed", "stale"]),
            )
        )
        or 0
    )

    missing_information = []
    for item in summary_json.get("missing_information", []) if isinstance(summary_json, dict) else []:
        if isinstance(item, dict):
            missing_information.append(item.get("field") or item.get("reason") or "unknown")
        elif isinstance(item, str):
            missing_information.append(item)

    capability_score = _estimate_capability_fit(opportunity, summary_json)
    product_score = 0.75 if summary_json.get("items") else 0.45

    lead_time_days = None
    if isinstance(summary_json.get("delivery"), dict):
        lead_time_days = summary_json["delivery"].get("delivery_days")

    amendment_count = 0
    if isinstance(summary_json.get("amendment_status"), dict):
        amendment_count = int(summary_json["amendment_status"].get("amendment_count") or 0)

    state = {
        "opportunity": {
            "id": opportunity.id,
            "source": opportunity.source,
            "source_id": opportunity.source_id,
            "title": opportunity.title,
            "status": opportunity.status,
            "psc": opportunity.psc_code,
            "naics": opportunity.naics_code,
            "set_aside": opportunity.set_aside_code,
            "nsn": opportunity.nsn,
            "response_deadline": opportunity.response_deadline.isoformat() if opportunity.response_deadline else None,
            "days_remaining": days_remaining,
            "required_delivery_days": lead_time_days,
            "estimated_value_min": _as_float(opportunity.estimated_value_min),
            "estimated_value_max": _as_float(opportunity.estimated_value_max),
        },
        "eligibility": {
            "sam_active": None,
            "set_aside_match": None if not opportunity.set_aside_code else True,
            "mandatory_certifications_met": None if not summary_json.get("certifications") else True,
        },
        "sourcing": {
            "product_found": bool(summary_json.get("items")),
            "supplier_count": None,
            "best_supplier_cost": _as_float(latest_pursuit.sourcing_cost) if latest_pursuit else None,
            "lead_time_days": lead_time_days,
        },
        "pricing": {
            "proposed_price": _as_float(latest_pursuit.quote_price) if latest_pursuit else None,
            "supplier_cost": _as_float(latest_pursuit.sourcing_cost) if latest_pursuit else None,
            "margin_pct": _as_float(latest_pursuit.margin_pct) if latest_pursuit else None,
            "historical_comparable_count": len(comparable_amounts),
            "historical_median": historical_median,
        },
        "compliance": {
            "mandatory_total": mandatory_total,
            "mandatory_missing": mandatory_missing,
            "needs_review": needs_review,
            "critical_total": critical_total,
            "critical_unresolved": critical_unresolved,
            "country_of_origin_conflict": _country_of_origin_conflict(summary_json),
        },
        "review": {
            "review_policy": latest_review.review_policy if latest_review else "conditional",
            "required_review_count": latest_review.required_review_count if latest_review else 1,
            "completed_review_count": latest_review.completed_review_count if latest_review else 0,
        },
        "analysis": {
            "summary": summary_json.get("summary"),
            "missing_information": missing_information,
            "amendment_count": amendment_count,
            "risk_flags": summary_json.get("risk_flags", []),
        },
        "amendment": {
            "count": amendment_count,
            "material": amendment_count > 0,
        },
        "outcome": {"evidence_count": 0, "no_bid_reason": None, "loss_reason": None},
        "scores": {
            "capability_fit_score": capability_score,
            "product_fit_score": product_score,
        },
        "signals": {
            "has_attachments": bool(summary_json),
            "requires_deep_analysis": bool(summary_json),
            "competitor_bucket_count": competitor_bucket_count,
            "incumbent_signal": competitor_bucket_count >= 2,
        },
        "source_snapshot_ids": source_snapshot_ids,
        "evidence_refs": _build_evidence_refs(opportunity, latest_summary, award_comps, source_snapshot_ids),
        "bundle_results": {},
    }
    return state


def run_decision_bundle(
    session: Session,
    *,
    opportunity_id: int,
    bundle_name: str,
    state: dict[str, Any] | None = None,
    settings: Settings | None = None,
    allow_llm_fallback: bool = False,
) -> BundleExecution:
    """Run one decision bundle and persist an immutable decision_runs row."""
    settings = settings or get_settings()
    definition = bundle_definition(bundle_name)
    state = state or build_decision_state(session, opportunity_id)
    state_hash = canonical_content_hash(json_safe(state))
    previous_run = session.scalar(
        select(DecisionRun)
        .where(DecisionRun.opportunity_id == opportunity_id, DecisionRun.bundle_name == bundle_name)
        .order_by(desc(DecisionRun.created_at), desc(DecisionRun.id))
        .limit(1)
    )

    hard_findings = evaluate_hard_rules(state)
    rule_provider = RuleDecisionProvider()
    baseline = rule_provider.decide(
        bundle_name=bundle_name,
        bundle_version=definition.version,
        state=state,
    )
    active: ProviderDecision = baseline

    primary = (settings.decision_primary_provider or "jev").strip().lower()
    fallback = (settings.decision_fallback_provider or "rules").strip().lower()
    jev_error: Exception | None = None
    if primary == "jev":
        try:
            jev_provider = JevDecisionProvider.from_settings(settings)
            jev_result = jev_provider.decide(
                bundle_name=bundle_name,
                bundle_version=definition.version,
                state=state,
            )
            merged = dict(baseline.result)
            merged.update(jev_result.result)
            active = ProviderDecision(
                provider=jev_result.provider,
                model=jev_result.model,
                result=merged,
                confidence=jev_result.confidence if jev_result.confidence is not None else baseline.confidence,
                cost=jev_result.cost,
                latency_ms=jev_result.latency_ms,
                raw_response=jev_result.raw_response,
            )
        except AIGatewayBlocked as exc:
            logger.info(
                "ai_gateway decision=block classification=%s provider=jev purpose=decision_bundle:%s action=fallback_to_rules",
                exc.classification.value,
                bundle_name,
            )
            jev_error = exc
        except DecisionProviderUnavailable as exc:
            jev_error = exc
    elif primary == "llm":
        try:
            llm = LLMDecisionProvider(settings=settings)
            llm_result = llm.decide(
                bundle_name=bundle_name,
                bundle_version=definition.version,
                state=state,
            )
            merged = dict(baseline.result)
            merged.update(llm_result.result)
            active = ProviderDecision(
                provider=llm_result.provider,
                model=llm_result.model,
                result=merged,
                confidence=llm_result.confidence if llm_result.confidence is not None else baseline.confidence,
                cost=llm_result.cost,
                latency_ms=llm_result.latency_ms,
                raw_response=llm_result.raw_response,
            )
        except DecisionProviderUnavailable:
            active = baseline

    if (
        active.provider == "rules"
        and (allow_llm_fallback or fallback == "llm")
        and (jev_error is not None or primary == "llm")
    ):
        try:
            llm = LLMDecisionProvider(settings=settings)
            llm_result = llm.decide(
                bundle_name=bundle_name,
                bundle_version=definition.version,
                state=state,
            )
            merged = dict(active.result)
            merged.update(llm_result.result)
            active = ProviderDecision(
                provider=llm_result.provider,
                model=llm_result.model,
                result=merged,
                confidence=llm_result.confidence if llm_result.confidence is not None else active.confidence,
                cost=llm_result.cost,
                latency_ms=llm_result.latency_ms,
                raw_response=llm_result.raw_response,
            )
        except DecisionProviderUnavailable:
            pass

    result_data = apply_hard_rule_override(bundle_name, active.result, hard_findings)
    threshold = settings.jev_human_review_threshold or 0.7
    result_data = enforce_low_confidence_escalation(
        bundle_name,
        result_data,
        confidence=active.confidence,
        threshold=threshold,
    )

    validated = validate_bundle_result(bundle_name, result_data).model_dump(mode="json")
    spec_meta = load_decision_spec_metadata(bundle_name, settings=settings)
    row = DecisionRun(
        opportunity_id=opportunity_id,
        bundle_name=bundle_name,
        bundle_version=definition.version,
        provider=active.provider,
        model=active.model,
        decision_spec_name=spec_meta.decision_spec_name,
        decision_spec_hash=spec_meta.content_hash,
        schema_version=definition.input_schema,
        input_state=json_safe(state),
        input_state_hash=state_hash,
        result=validated,
        confidence=Decimal(str(active.confidence)) if active.confidence is not None else None,
        cost=active.cost,
        latency_ms=active.latency_ms,
        source_snapshot_ids=state.get("source_snapshot_ids") or None,
        supersedes_run_id=previous_run.id if previous_run else None,
    )
    session.add(row)
    session.flush()
    return BundleExecution(
        bundle_name=bundle_name,
        run=row,
        result=validated,
        provider=active.provider,
        model=active.model,
        confidence=active.confidence,
        hard_rule_findings=tuple(item.reason for item in hard_findings),
    )


def run_preliminary_decision_package(
    session: Session,
    *,
    opportunity_id: int,
    settings: Settings | None = None,
    allow_llm_fallback: bool = False,
) -> DecisionPackageExecution:
    """Run the Phase 8 acceptance bundles and persist a decision package."""
    settings = settings or get_settings()
    state = build_decision_state(session, opportunity_id)
    bundle_order = [
        "opportunity_triage",
        "eligibility_and_execution",
        "market_and_pricing",
        "bid_decision",
        "compliance_and_amendment",
        "collaborative_review_synthesis",
        "proposal_review",
        "outcome_learning",
    ]
    runs: list[BundleExecution] = []
    for bundle_name in bundle_order:
        execution = run_decision_bundle(
            session,
            opportunity_id=opportunity_id,
            bundle_name=bundle_name,
            state=state,
            settings=settings,
            allow_llm_fallback=allow_llm_fallback,
        )
        runs.append(execution)
        state["bundle_results"][bundle_name] = execution.result

    recommendation = _build_preliminary_recommendation(state, state["bundle_results"])
    bid_bundle = state["bundle_results"]["bid_decision"]
    market_bundle = state["bundle_results"]["market_and_pricing"]
    eligibility_bundle = state["bundle_results"]["eligibility_and_execution"]
    compliance_bundle = state["bundle_results"]["compliance_and_amendment"]

    bid_decision = BidDecision(
        opportunity_id=opportunity_id,
        recommendation=recommendation.recommendation,
        recommendation_score=Decimal(str(recommendation.score)),
        capability_score=Decimal(str(state["scores"]["capability_fit_score"])) if state["scores"]["capability_fit_score"] is not None else None,
        pricing_score=Decimal(str(_score_from_level(market_bundle.get("commercial_attractiveness")))),
        past_performance_score=None,
        deadline_score=Decimal(
            str(
                _score_from_level(
                    state["bundle_results"]["opportunity_triage"].get("deadline_risk"),
                    levels=("low", "medium", "high", "critical"),
                    invert=True,
                )
            )
        ),
        competition_score=Decimal(str(_score_from_level(market_bundle.get("competition_level"), levels=("low", "medium", "high")))),
        margin_score=Decimal(str(_score_from_level(market_bundle.get("margin_quality"), levels=("poor", "marginal", "good", "strong")))),
        compliance_risk_score=Decimal(
            str(
                _score_from_level(
                    bid_bundle.get("compliance_risk"),
                    levels=("low", "medium", "high", "critical"),
                    invert=True,
                )
            )
        ),
        strengths={"items": recommendation.strengths},
        risks={"items": recommendation.risks},
        missing_information={"items": recommendation.missing_information},
        evidence={"items": recommendation.evidence},
        rules_result={"hard_rule_blockers": bid_bundle.get("hard_rule_blockers", [])},
        jev_result=_find_provider_payload(runs, "jev"),
        llm_result=_find_provider_payload(runs, "llm"),
        human_decision=None,
        human_comments=None,
    )
    session.add(bid_decision)
    session.flush()

    next_state = "READY_FOR_COLLABORATIVE_REVIEW"
    if recommendation.recommendation in {"no_bid", "insufficient_information"}:
        next_state = "REVIEW_REQUIRED"

    package_output = {
        "opportunity_summary": {
            "id": opportunity_id,
            "title": state["opportunity"]["title"],
            "source": state["opportunity"]["source"],
            "deadline": state["opportunity"]["response_deadline"],
            "days_remaining": state["opportunity"]["days_remaining"],
            "psc": state["opportunity"]["psc"],
            "naics": state["opportunity"]["naics"],
            "set_aside": state["opportunity"]["set_aside"],
        },
        "eligibility_status": eligibility_bundle,
        "capability_fit": {
            "level": eligibility_bundle.get("capability_match"),
            "score": state["scores"].get("capability_fit_score"),
        },
        "product_source_status": state["sourcing"],
        "supplier_findings": {"supplier_count": state["sourcing"].get("supplier_count"), "lead_time_days": state["sourcing"].get("lead_time_days")},
        "historical_award_pricing_intelligence": market_bundle,
        "expected_margin_pricing_position": {
            "margin_pct": state["pricing"].get("margin_pct"),
            "margin_quality": market_bundle.get("margin_quality"),
            "historical_median": state["pricing"].get("historical_median"),
        },
        "compliance_matrix_summary": compliance_bundle,
        "deadline_risk": state["bundle_results"]["opportunity_triage"].get("deadline_risk"),
        "execution_risk": bid_bundle.get("execution_risk"),
        "competition_incumbent_signals": {
            "competition_level": market_bundle.get("competition_level"),
            "incumbent_advantage": market_bundle.get("incumbent_advantage"),
        },
        "jev_preliminary_recommendation": bid_bundle,
        "why_bid": recommendation.strengths,
        "why_no_bid": recommendation.risks,
        "missing_information": recommendation.missing_information,
        "open_questions": state["analysis"].get("missing_information", []),
        "source_evidence": recommendation.evidence,
        "bundle_runs": [
            {
                "bundle_name": run.bundle_name,
                "decision_run_id": run.run.id,
                "provider": run.provider,
                "model": run.model,
                "confidence": run.confidence,
            }
            for run in runs
        ],
        "next_state": next_state,
        "human_decision_authority": "required",
        "bid_decision_id": bid_decision.id,
        "guardrails": {
            "bid_approval_requires_human_decision": True,
            "submission_requires_human_decision": True,
        },
    }
    analysis = AIAnalysis(
        opportunity_id=opportunity_id,
        analysis_type="decision_package",
        provider="decision_engine",
        model=runs[-1].model if runs else None,
        prompt_name="jev_decision_package",
        prompt_version="v1",
        prompt_hash=canonical_content_hash(
            {"decision_specs": [run.run.decision_spec_hash for run in runs if run.run.decision_spec_hash]}
        ),
        schema_version="decision_package.v1",
        generation_settings={
            "decision_primary_provider": settings.decision_primary_provider,
            "decision_fallback_provider": settings.decision_fallback_provider,
        },
        input_snapshot_hash=canonical_content_hash(json_safe(state)),
        context_manifest={
            "opportunity_id": opportunity_id,
            "source_snapshot_ids": state.get("source_snapshot_ids", []),
            "decision_run_ids": [run.run.id for run in runs],
        },
        output_json=package_output,
        source_refs={"evidence": recommendation.evidence},
        token_usage=None,
        estimated_cost=None,
        latency_ms=sum(run.run.latency_ms or 0 for run in runs),
    )
    session.add(analysis)
    session.flush()

    review_session = session.scalar(select(ReviewSession).where(ReviewSession.opportunity_id == opportunity_id))
    if review_session is None:
        review_session = ReviewSession(
            opportunity_id=opportunity_id,
            status="ready_for_review",
            ai_decision_package_id=analysis.id,
            review_policy="conditional",
            required_review_count=1,
            completed_review_count=0,
        )
        session.add(review_session)
    else:
        review_session.ai_decision_package_id = analysis.id
        if review_session.status == "pending":
            review_session.status = "ready_for_review"
    session.flush()

    return DecisionPackageExecution(
        analysis=analysis,
        bid_decision=bid_decision,
        bundle_runs=tuple(runs),
        package_output=package_output,
    )


def list_decision_runs(
    session: Session,
    *,
    opportunity_id: int,
    bundle_name: str | None = None,
    limit: int = 25,
) -> list[DecisionRun]:
    query = select(DecisionRun).where(DecisionRun.opportunity_id == opportunity_id)
    if bundle_name:
        query = query.where(DecisionRun.bundle_name == bundle_name)
    rows = session.scalars(query.order_by(desc(DecisionRun.created_at), desc(DecisionRun.id)).limit(limit)).all()
    return list(rows)


def _build_preliminary_recommendation(
    state: dict[str, Any],
    bundle_results: dict[str, dict[str, Any]],
) -> PreliminaryRecommendation:
    bid = bundle_results["bid_decision"]
    market = bundle_results["market_and_pricing"]
    eligibility = bundle_results["eligibility_and_execution"]
    compliance = bundle_results["compliance_and_amendment"]
    strengths: list[str] = []
    risks: list[str] = []
    missing = list(state.get("analysis", {}).get("missing_information", []))
    if eligibility.get("capability_match") in {"high", "very_high"}:
        strengths.append("Capability fit is strong.")
    if market.get("margin_quality") in {"good", "strong"}:
        strengths.append("Expected margin profile is commercially attractive.")
    if market.get("historical_comparability") == "high":
        strengths.append("Historical award comparability is strong.")
    if bid.get("execution_risk") in {"high", "critical"}:
        risks.append("Execution risk is elevated.")
    if bid.get("compliance_risk") in {"high", "critical"}:
        risks.append("Compliance risk remains high.")
    for blocker in bid.get("hard_rule_blockers", []):
        risks.append(blocker)
    if not strengths:
        strengths.append("No strong automated strengths identified.")
    if not risks:
        risks.append("No decisive blocking risk identified.")

    evidence = list(state.get("evidence_refs", []))
    factor_scores = {
        "capability_fit": state.get("scores", {}).get("capability_fit_score"),
        "product_source_availability": 1.0 if state.get("sourcing", {}).get("product_found") else 0.0,
        "historical_pricing": _score_from_level(market.get("historical_comparability"), levels=("low", "medium", "high")),
        "expected_margin": _score_from_level(market.get("margin_quality"), levels=("poor", "marginal", "good", "strong")),
        "past_performance_fit": None,
        "delivery_feasibility": 1.0 if bundle_results["eligibility_and_execution"].get("delivery_feasibility") == "yes" else 0.0,
        "competition": _score_from_level(market.get("competition_level"), levels=("low", "medium", "high"), invert=True),
        "set_aside_eligibility": 1.0 if state.get("eligibility", {}).get("set_aside_match") in (True, None) else 0.0,
        "compliance_risk": _score_from_level(
            bid.get("compliance_risk"),
            levels=("low", "medium", "high", "critical"),
            invert=True,
        ),
        "deadline_risk": _score_from_level(
            bundle_results["opportunity_triage"].get("deadline_risk"),
            levels=("low", "medium", "high", "critical"),
            invert=True,
        ),
        "submission_complexity": _score_from_level(compliance.get("false_satisfied_risk"), levels=("low", "medium", "high"), invert=True),
    }
    return PreliminaryRecommendation(
        recommendation=bid["recommendation"],
        score=confidence_to_score(bid.get("evidence_confidence")),
        strengths=strengths,
        risks=risks,
        missing_information=missing,
        evidence=evidence,
        factor_scores=factor_scores,
        human_review_required=bool(bid.get("human_review_required", True)),
    )


def _find_provider_payload(runs: list[BundleExecution], provider: str) -> dict[str, Any] | None:
    for run in runs:
        if run.provider == provider:
            return {
                "bundle_name": run.bundle_name,
                "decision_run_id": run.run.id,
                "confidence": run.confidence,
            }
    return None


def _build_evidence_refs(
    opportunity: Opportunity,
    latest_summary: AIAnalysis | None,
    award_comps: list[Any],
    source_snapshot_ids: list[int],
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = [
        {
            "source": "opportunity",
            "opportunity_id": opportunity.id,
            "response_deadline": opportunity.response_deadline.isoformat() if opportunity.response_deadline else None,
            "snapshot_ids": source_snapshot_ids,
        }
    ]
    if latest_summary is not None:
        evidence.append(
            {
                "source": "solicitation_summary",
                "ai_analysis_id": latest_summary.id,
                "schema_version": latest_summary.schema_version,
            }
        )
    if award_comps:
        evidence.append(
            {
                "source": "award_history",
                "comparable_award_count": len(award_comps),
                "sample_award_ids": [item.award_id for item in award_comps[:3]],
            }
        )
    return evidence


def _estimate_capability_fit(opportunity: Opportunity, summary_json: dict[str, Any]) -> float:
    score = 0.45
    if opportunity.psc_code:
        score += 0.1
    if opportunity.naics_code:
        score += 0.1
    if summary_json.get("items"):
        score += 0.15
    if summary_json.get("evaluation_factors"):
        score += 0.05
    return min(score, 0.95)


def _country_of_origin_conflict(summary_json: dict[str, Any]) -> bool:
    refs = summary_json.get("country_of_origin_references")
    if not isinstance(refs, list):
        return False
    lowered = " ".join(str(item).lower() for item in refs)
    return "prohibited" in lowered or "restricted" in lowered


def _as_float(value: Any) -> float | None:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _score_from_level(
    level: str | None,
    *,
    levels: tuple[str, ...] = ("very_low", "low", "medium", "high", "very_high"),
    invert: bool = False,
) -> float:
    if level is None:
        return 0.5
    normalized = str(level).lower()
    if normalized not in levels:
        return 0.5
    idx = levels.index(normalized)
    score = idx / max(len(levels) - 1, 1)
    return round(1.0 - score if invert else score, 4)
