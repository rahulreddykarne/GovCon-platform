"""AI validation for collaborative-review comments."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from govcon.ai.schemas import ReviewerCommentValidationV1
from govcon.ai.structured import StructuredCallError, run_structured_prompt
from govcon.compliance.metrics import coverage_counts
from govcon.compliance.matrix import active_requirements, open_findings
from govcon.matching.pricing import recent_award_comps
from govcon.models import AIAnalysis, Pursuit, ReviewComment, ReviewSession


@dataclass(frozen=True)
class AICommentValidationError(RuntimeError):
    reason: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - trivial repr
        return f"{self.reason}: {self.detail}"


def validate_comment_with_ai(
    session: Session, *, comment: ReviewComment
) -> ReviewerCommentValidationV1:
    variables = {
        "REVIEWER_COMMENT_JSON": {
            "comment_id": comment.id,
            "topic": comment.topic,
            "body": comment.body,
            "source_refs": comment.source_refs,
            "user_recommendation": comment.user_recommendation,
        },
        "AI_DECISION_PACKAGE_JSON": _decision_package_payload(
            session, opportunity_id=comment.opportunity_id
        ),
        "SOLICITATION_EVIDENCE_JSON": _solicitation_payload(
            session, opportunity_id=comment.opportunity_id
        ),
        "SUPPLIER_PRICING_EVIDENCE_JSON": _supplier_pricing_payload(
            session, opportunity_id=comment.opportunity_id
        ),
        "COMPLIANCE_STATE_JSON": _compliance_payload(
            session, opportunity_id=comment.opportunity_id
        ),
        "HISTORICAL_AWARD_EVIDENCE_JSON": _award_payload(
            session, opportunity_id=comment.opportunity_id
        ),
    }
    try:
        result = run_structured_prompt(
            session,
            opportunity_id=comment.opportunity_id,
            prompt_name="reviewer_comment_validation",
            analysis_type="reviewer_comment_validation",
            variables=variables,
            context_manifest={
                "opportunity_id": comment.opportunity_id,
                "comment_id": comment.id,
            },
        )
    except StructuredCallError as exc:
        raise AICommentValidationError(exc.reason, exc.detail) from exc
    return ReviewerCommentValidationV1.model_validate(result.output.model_dump(mode="json"))


def apply_ai_validation_to_comment(
    comment: ReviewComment, validation: ReviewerCommentValidationV1
) -> None:
    comment.ai_position = validation.position
    comment.ai_confidence = validation.confidence
    comment.ai_reason = validation.reason
    comment.ai_supporting_evidence = [
        evidence.model_dump(mode="json") for evidence in validation.supporting_evidence
    ]
    comment.ai_contradicting_evidence = [
        evidence.model_dump(mode="json")
        for evidence in validation.contradicting_evidence
    ]
    comment.ai_missing_information = list(validation.missing_information)
    comment.ai_suggested_action = validation.suggested_action


def _decision_package_payload(session: Session, *, opportunity_id: int) -> dict[str, Any]:
    review = session.scalar(
        select(ReviewSession).where(ReviewSession.opportunity_id == opportunity_id)
    )
    analysis: AIAnalysis | None = None
    if review and review.ai_decision_package_id:
        analysis = session.get(AIAnalysis, review.ai_decision_package_id)
    if analysis is None:
        analysis = session.scalar(
            select(AIAnalysis)
            .where(
                AIAnalysis.opportunity_id == opportunity_id,
                AIAnalysis.analysis_type == "decision_package",
            )
            .order_by(desc(AIAnalysis.created_at), desc(AIAnalysis.id))
            .limit(1)
        )
    return analysis.output_json if analysis and isinstance(analysis.output_json, dict) else {}


def _solicitation_payload(session: Session, *, opportunity_id: int) -> dict[str, Any]:
    summary = session.scalar(
        select(AIAnalysis)
        .where(
            AIAnalysis.opportunity_id == opportunity_id,
            AIAnalysis.analysis_type == "solicitation_summary",
        )
        .order_by(desc(AIAnalysis.created_at), desc(AIAnalysis.id))
        .limit(1)
    )
    return summary.output_json if summary and isinstance(summary.output_json, dict) else {}


def _supplier_pricing_payload(session: Session, *, opportunity_id: int) -> dict[str, Any]:
    pursuit = session.scalar(
        select(Pursuit).where(Pursuit.opportunity_id == opportunity_id).limit(1)
    )
    return {
        "sourcing_cost": float(pursuit.sourcing_cost) if pursuit and pursuit.sourcing_cost is not None else None,
        "quote_price": float(pursuit.quote_price) if pursuit and pursuit.quote_price is not None else None,
        "margin_pct": float(pursuit.margin_pct) if pursuit and pursuit.margin_pct is not None else None,
        "supplier": pursuit.supplier if pursuit else None,
    }


def _compliance_payload(session: Session, *, opportunity_id: int) -> dict[str, Any]:
    requirements = active_requirements(session, opportunity_id)
    findings = open_findings(session, opportunity_id)
    counts = coverage_counts(requirements, findings)
    return {
        "counts": counts,
        "open_findings": [
            {
                "finding_type": finding.finding_type,
                "severity": finding.severity,
                "description": finding.description,
                "blocks_submission": finding.blocks_submission,
            }
            for finding in findings
        ],
    }


def _award_payload(session: Session, *, opportunity_id: int) -> dict[str, Any]:
    from govcon.models import Opportunity

    opportunity = session.get(Opportunity, opportunity_id)
    if opportunity is None:
        return {"comparables": []}
    comps = recent_award_comps(
        session, nsn=opportunity.nsn, psc_code=opportunity.psc_code, limit=10
    )
    return {
        "comparables": [
            {
                "award_id": item.award_id,
                "vendor": item.vendor,
                "amount": float(item.amount) if item.amount is not None else None,
                "unit_price": float(item.unit_price)
                if item.unit_price is not None
                else None,
                "action_date": item.action_date.isoformat()
                if item.action_date is not None
                else None,
            }
            for item in comps
        ]
    }
