"""Phase 8 decision orchestration and persistence."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from govcon.ai.analysis_types import AnalysisType
from govcon.config import Settings, get_settings
from govcon.decision.bundles import bundle_definition, load_decision_spec_metadata
from govcon.decision.provider import (
    DecisionProviderUnavailable,
    ProviderDecision,
)
from govcon.decision.providers import (
    JevDecisionProvider,
    LLMDecisionProvider,
    RuleDecisionProvider,
)
from govcon.decision.rules import (
    apply_hard_rule_override,
    confidence_to_score,
    enforce_low_confidence_escalation,
    evaluate_hard_rules,
)
from govcon.decision.schemas import PreliminaryRecommendation, validate_bundle_result
from govcon.decision.signals import (
    Signals,
    amendment_signal,
    capability_signal,
    competition_signals,
    eligibility_signals,
    load_company_profile,
    pricing_signals,
    sourcing_signals,
    supplier_lead_time_signal,
)
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
    StoredFile,
)
from govcon.workflow.source_revision import (
    SOURCE_REVISION_KEY,
    current_source_revision,
    is_stale,
    stamp_of,
)

logger = logging.getLogger("govcon.decision.engine")


@dataclass(frozen=True)
class BundleExecution:
    bundle_name: str
    run: DecisionRun
    result: dict[str, Any]
    provider: str
    model: str | None
    confidence: float | None
    hard_rule_findings: tuple[str, ...]
    fallback_reason: str | None = None


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
            AIAnalysis.analysis_type == AnalysisType.SOLICITATION_SUMMARY,
        )
        .order_by(desc(AIAnalysis.created_at), desc(AIAnalysis.id))
        .limit(1)
    )
    source_revision = current_source_revision(session, opportunity_id)
    summary_stale = latest_summary is not None and is_stale(
        stamp_of(latest_summary.context_manifest), source_revision
    )
    if summary_stale:
        # Analysis of a superseded source must not drive the decision.
        latest_summary = None
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
    if summary_stale:
        missing_information.append("solicitation analysis is stale: the source changed after it ran")
    elif latest_summary is None:
        missing_information.append("solicitation analysis has not run")
    for item in summary_json.get("missing_information", []) if isinstance(summary_json, dict) else []:
        if isinstance(item, dict):
            missing_information.append(item.get("field") or item.get("reason") or "unknown")
        elif isinstance(item, str):
            missing_information.append(item)

    # The buyer's required delivery period (a requirement, not supplier evidence).
    required_delivery_days = None
    if isinstance(summary_json.get("delivery"), dict):
        required_delivery_days = summary_json["delivery"].get("delivery_days")

    amendment_count = 0
    if isinstance(summary_json.get("amendment_status"), dict):
        amendment_count = int(summary_json["amendment_status"].get("amendment_count") or 0)

    # Evidence-backed, three-state signals with provenance (see decision.signals).
    profile = load_company_profile(session=session)
    signals = Signals()
    eligibility_signals(
        session, opportunity, profile, summary_json, has_summary=latest_summary is not None, signals=signals
    )
    sourcing_signals(latest_pursuit, signals)
    supplier_lead_time_signal(session, opportunity_id, signals)
    capability_signal(session, opportunity, profile, signals)
    from govcon.sourcing.product_facts import product_facts_from_summary

    product_facts = product_facts_from_summary(opportunity, latest_summary)
    pricing_signals(opportunity, latest_pursuit, award_comps, signals, product_quantity=product_facts.quantity)
    competition_signals(comp_summary, signals)
    amendment_signal(session, opportunity_id, amendment_count, signals)
    sig = signals.values
    for name in ("set_aside_match", "sam_active", "mandatory_certifications_met", "product_found", "capability_fit_score"):
        if sig.get(name) is None:
            missing_information.append(f"{name} is unknown ({signals.provenance[name].get('detail') or 'no evidence'})")
    stored_files = int(
        session.scalar(
            select(func.count())
            .select_from(StoredFile)
            .where(StoredFile.opportunity_id == opportunity_id, StoredFile.active.is_(True), StoredFile.sha256.is_not(None))
        )
        or 0
    )

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
            "required_delivery_days": required_delivery_days,
            "estimated_value_min": _as_float(opportunity.estimated_value_min),
            "estimated_value_max": _as_float(opportunity.estimated_value_max),
        },
        "eligibility": {
            "sam_active": sig["sam_active"],
            "set_aside_match": sig["set_aside_match"],
            "mandatory_certifications_met": sig["mandatory_certifications_met"],
        },
        "sourcing": {
            "product_found": sig["product_found"],
            "supplier_count": 1 if sig["product_found"] else None,
            "best_supplier_cost": _as_float(latest_pursuit.sourcing_cost) if latest_pursuit else None,
            # Verified supplier evidence only; None (unknown) when absent.
            "lead_time_days": sig["supplier_lead_time_days"],
        },
        "pricing": {
            "proposed_price": _as_float(latest_pursuit.quote_price) if latest_pursuit else None,
            "supplier_cost": _as_float(latest_pursuit.sourcing_cost) if latest_pursuit else None,
            "margin_pct": _as_float(latest_pursuit.margin_pct) if latest_pursuit else None,
            # Only awards that state a unit price are comparable to a quote.
            "historical_comparable_count": sig["historical_unit_price_count"],
            "historical_median_unit_price": sig["historical_median_unit_price"],
            "proposed_unit_price": sig["proposed_unit_price"],
            "unit_cost": sig["unit_cost"],
            # Whole-contract totals are context only, never a price benchmark.
            "historical_median_award_total": sig["historical_median_award_total"],
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
            "material": sig["amendment_material"] is True,
            "material_known": sig["amendment_material"] is not None,
        },
        "outcome": {"evidence_count": 0, "no_bid_reason": None, "loss_reason": None},
        "scores": {
            "capability_fit_score": sig["capability_fit_score"],
            "product_fit_score": 0.75 if sig["product_found"] else None,
        },
        "signals": {
            "has_attachments": stored_files > 0,
            "requires_deep_analysis": stored_files > 0,
            "competitor_bucket_count": competitor_bucket_count,
            "distinct_awardee_count": sig["distinct_awardee_count"],
            # Heuristic: an awardee with 2+ comparable awards. Not a verified incumbent.
            "repeat_awardee_signal": sig["repeat_awardee_signal"],
        },
        "provenance": signals.provenance,
        "source_snapshot_ids": source_snapshot_ids,
        SOURCE_REVISION_KEY: source_revision,
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
    from govcon.security.classification import (
        DataClassification,
        opportunity_classification,
    )
    state = dict(state)
    state["data_classification"] = opportunity_classification(session, opportunity_id, DataClassification.PROPRIETARY).value
    state["budget_opportunity_id"] = opportunity_id
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

    from govcon.ai.routing import decision_providers

    primary, fallback = decision_providers(session, settings)
    jev_error: Exception | None = None
    if primary == "jev":
        try:
            jev_provider = JevDecisionProvider.from_settings(settings, session=session)
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
        except DecisionProviderUnavailable as exc:
            jev_error = exc
            logger.warning(
                "JEV unavailable for bundle=%s opportunity=%s; using the rules provider: %s",
                bundle_name,
                opportunity_id,
                exc,
            )
    elif primary == "llm":
        try:
            llm = LLMDecisionProvider(settings=settings, session=session)
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
        except DecisionProviderUnavailable as exc:
            logger.warning("LLM decision provider unavailable for bundle=%s; using rules: %s", bundle_name, exc)
            active = baseline

    if (
        active.provider == "rules"
        and (allow_llm_fallback or fallback == "llm")
        and (jev_error is not None or primary == "llm")
    ):
        try:
            llm = LLMDecisionProvider(settings=settings, session=session)
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
    fallback_reason = None
    if jev_error is not None and active.provider == "rules":
        # The rules result above is the baseline, including compliance findings.
        # The exception type is recorded; the message can contain a URL.
        fallback_reason = f"JEV unavailable ({type(jev_error).__name__}); rules result kept"
    return BundleExecution(
        bundle_name=bundle_name,
        run=row,
        result=validated,
        provider=active.provider,
        model=active.model,
        confidence=active.confidence,
        hard_rule_findings=tuple(item.reason for item in hard_findings),
        fallback_reason=fallback_reason,
    )


def _rules_payload(bid_bundle: dict[str, Any], runs: list[BundleExecution]) -> dict[str, Any]:
    payload: dict[str, Any] = {"hard_rule_blockers": bid_bundle.get("hard_rule_blockers", [])}
    reasons = [run.fallback_reason for run in runs if run.fallback_reason]
    if reasons:
        payload["fallback"] = {"from": "jev", "to": "rules", "why": reasons}
    return payload


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
    prior_bid = session.scalar(
        select(BidDecision)
        .where(BidDecision.opportunity_id == opportunity_id)
        .order_by(desc(BidDecision.created_at), desc(BidDecision.id))
        .limit(1)
    )
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
        rules_result=_rules_payload(bid_bundle, runs),
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
        analysis_type=AnalysisType.DECISION_PACKAGE,
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
            SOURCE_REVISION_KEY: state.get(SOURCE_REVISION_KEY),
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
        if (
            review_session.status == "approved_to_bid"
            and prior_bid is not None
            and prior_bid.recommendation != recommendation.recommendation
        ):
            # The approval was given against a different recommendation.
            from govcon.compliance.matrix import upsert_open_finding

            upsert_open_finding(
                session,
                opportunity_id=opportunity_id,
                finding_type="review_reopen_required",
                severity="high",
                description=(
                    f"The regenerated decision package recommends {recommendation.recommendation!r} "
                    f"but the bid was approved against {prior_bid.recommendation!r}; reopen the review."
                ),
                detected_by="decision_engine",
                detector_version="decision_package.v1",
                source_refs={"prior_bid_decision_id": prior_bid.id, "bid_decision_id": bid_decision.id},
                blocks_submission=True,
            )
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
    missing_information = state.get("analysis", {}).get("missing_information") or []
    missing = missing_information.copy() if isinstance(missing_information, list) else list(missing_information)
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
    risks.extend(bid.get("hard_rule_blockers") or [])
    if not strengths:
        strengths.append("No strong automated strengths identified.")
    if not risks:
        risks.append("No decisive blocking risk identified.")

    evidence_refs = state.get("evidence_refs") or []
    evidence = evidence_refs.copy() if isinstance(evidence_refs, list) else list(evidence_refs)
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
