"""Review quorum, synthesis, and approval gate orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from govcon.ai.analysis_types import AnalysisType
from govcon.audit import record_audit
from govcon.collaboration.assignments import (
    assignment_for_user,
    list_assignments,
    start_assignment,
)
from govcon.collaboration.comments import list_comments, reviewer_comment_count
from govcon.collaboration.notifications import notify
from govcon.collaboration.users import require_permission
from govcon.compliance.matrix import open_findings
from govcon.concurrency import StaleRecordError, apply_versioned_update
from govcon.config import get_settings
from govcon.decision.engine import build_decision_state, run_decision_bundle
from govcon.models import (
    AIAnalysis,
    BidDecision,
    Opportunity,
    Pursuit,
    Requirement,
    ReviewAssignment,
    ReviewSession,
    User,
)
from govcon.security.classification import DataClassification
from govcon.workflow.invalidation import (
    DECIDED_REVIEW_STATES,
    apply_source_change,
    invalidate_proposal_approval,
    invalidate_submission_readiness,
    lock_one,
    lock_opportunity,
)
from govcon.workflow.source_revision import current_source_revision, is_stale, stamp_of
from govcon.workflow.transitions import InvalidTransition, can_transition, require_transition


class ReviewWorkflowError(RuntimeError):
    """A review lifecycle transition is invalid for the current state."""


@dataclass(frozen=True)
class QuorumState:
    review_policy: str
    required_review_count: int
    completed_review_count: int
    second_review_required: bool
    second_review_reason: str | None
    triggers: list[str]
    quorum_satisfied: bool
    reviewed_by_user_ids: list[int]
    pending_user_ids: list[int]


def ensure_review_session(
    session: Session,
    *,
    opportunity_id: int,
    review_policy: str = "conditional",
) -> ReviewSession:
    row = session.scalar(
        select(ReviewSession).where(ReviewSession.opportunity_id == opportunity_id)
    )
    if row is not None:
        return row
    row = ReviewSession(
        opportunity_id=opportunity_id,
        status="ready_for_review",
        review_policy=review_policy,
        required_review_count=1,
        completed_review_count=0,
    )
    session.add(row)
    session.flush()
    return row


def set_review_policy(
    session: Session,
    *,
    opportunity_id: int,
    review_policy: str,
    actor: User,
) -> ReviewSession:
    require_permission(actor, "approve")
    row = ensure_review_session(session, opportunity_id=opportunity_id)
    if review_policy not in {"single", "dual", "conditional"}:
        raise ValueError("review_policy must be single, dual, or conditional")
    old_policy = row.review_policy
    row.review_policy = review_policy
    row.updated_at = datetime.now(UTC)
    record_audit(
        session,
        action_type="review_policy_changed",
        user_id=actor.id,
        opportunity_id=opportunity_id,
        entity_type="review_sessions",
        entity_id=row.id,
        old_value={"review_policy": old_policy},
        new_value={"review_policy": review_policy},
    )
    recalculate_quorum(session, opportunity_id=opportunity_id)
    return row


def complete_assignment(
    session: Session,
    *,
    opportunity_id: int,
    user_id: int,
    action: str,
    recommendation: str | None = None,
    agree_with_ai_assessment: bool | None = None,
    second_review_reason: str | None = None,
) -> ReviewAssignment:
    """Complete the calling reviewer's own assignment.

    The reviewer must be active and hold the ``review`` permission. The review
    session and assignment rows are locked so concurrent completions serialize.
    Completing an already-complete assignment is a no-op, so a repeated submit
    counts once toward quorum.
    """
    if action not in {
        "approve_continue",
        "request_second_review",
        "return_for_ai_analysis",
        "no_bid",
    }:
        raise ValueError("unknown review action")
    user = session.get(User, user_id)
    if user is None or not user.is_active:
        raise ReviewWorkflowError("an active reviewer account is required")
    require_permission(user, "review")

    review = _locked_review(session, opportunity_id)
    if review.status in DECIDED_REVIEW_STATES:
        raise ReviewWorkflowError(
            f"review session is already {review.status}; it must be reopened before reviews change"
        )
    assignment = lock_one(
        session,
        select(ReviewAssignment).where(
            ReviewAssignment.opportunity_id == opportunity_id,
            ReviewAssignment.user_id == user_id,
        ),
    )
    if assignment is None:
        raise ReviewWorkflowError("review assignment not found")
    if assignment.status == "complete":
        return assignment

    start_assignment(session, opportunity_id=opportunity_id, user_id=user_id)
    comment_count = reviewer_comment_count(
        session, opportunity_id=opportunity_id, user_id=user_id
    )
    if (
        comment_count == 0
        and agree_with_ai_assessment is not True
        and action in {"approve_continue", "request_second_review", "no_bid"}
    ):
        raise ReviewWorkflowError(
            "reviewer must add a comment or explicitly agree_with_ai_assessment=true"
        )

    assignment.recommendation = recommendation
    assignment.agree_with_ai_assessment = agree_with_ai_assessment
    now = datetime.now(UTC)
    if action == "request_second_review":
        assignment.second_review_requested = True
        assignment.second_review_request_reason = (second_review_reason or "").strip() or None
    if action == "return_for_ai_analysis":
        assignment.status = "reopened"
        assignment.reopened_at = now
        assignment.completed_at = None
    else:
        assignment.status = "complete"
        assignment.completed_at = now
    session.flush()

    if action == "return_for_ai_analysis":
        if can_transition("review_session", review.status, "returned_for_review"):
            review.status = "returned_for_review"
        review.final_approval_status = "returned_for_review"
        review.ai_consolidated_review = None
        review.version = (review.version or 1) + 1
    session.flush()

    record_audit(
        session,
        action_type="review_assignment_completed",
        user_id=user_id,
        opportunity_id=opportunity_id,
        entity_type="review_assignments",
        entity_id=assignment.id,
        new_value={
            "action": action,
            "recommendation": recommendation,
            "agree_with_ai_assessment": agree_with_ai_assessment,
            "second_review_requested": assignment.second_review_requested,
            "second_review_request_reason": assignment.second_review_request_reason,
        },
    )
    for approver in _approvers(session):
        notify(
            session,
            user_id=approver.id,
            opportunity_id=opportunity_id,
            notification_type="reviewer_completed",
            payload={"assignment_id": assignment.id, "action": action, "reviewer_user_id": user_id},
        )
    recalculate_quorum(session, opportunity_id=opportunity_id)
    return assignment


def reopen_assignment(
    session: Session,
    *,
    opportunity_id: int,
    user_id: int,
    actor: User,
    reason: str | None = None,
) -> ReviewAssignment:
    require_permission(actor, "review")
    assignment = assignment_for_user(
        session, opportunity_id=opportunity_id, user_id=user_id
    )
    if assignment is None:
        raise ReviewWorkflowError("review assignment not found")
    old = {"status": assignment.status, "completed_at": assignment.completed_at}
    assignment.status = "reopened"
    assignment.reopened_at = datetime.now(UTC)
    assignment.completed_at = None
    session.flush()
    record_audit(
        session,
        action_type="review_assignment_reopened",
        user_id=actor.id,
        opportunity_id=opportunity_id,
        entity_type="review_assignments",
        entity_id=assignment.id,
        old_value=old,
        new_value={"status": assignment.status, "reason": reason},
    )
    recalculate_quorum(session, opportunity_id=opportunity_id)
    return assignment


def apply_material_amendment_reopen(
    session: Session,
    *,
    opportunity_id: int,
    actor: User | None = None,
) -> bool:
    """Consume an open ``review_reopen_required`` finding.

    Reopens completed reviews and decided sessions (including override-only
    approvals with no completed assignments), and invalidates the proposal
    approval, submission readiness, and pursuit stage that depended on them.
    The finding stays open until a human resolves it.
    """
    review = session.scalar(
        select(ReviewSession).where(ReviewSession.opportunity_id == opportunity_id)
    )
    if review is None:
        return False
    should_reopen = any(
        finding.finding_type == "review_reopen_required"
        for finding in open_findings(session, opportunity_id)
    )
    if not should_reopen:
        return False
    actor_id = actor.id if actor is not None else None
    applied = apply_source_change(
        session,
        opportunity_id,
        level="material",
        reason="material_amendment_after_review",
        actor_id=actor_id,
    )
    changed = any(applied.values())
    if changed:
        record_audit(
            session,
            action_type="review_reopened_for_amendment",
            user_id=actor_id,
            opportunity_id=opportunity_id,
            entity_type="review_sessions",
            entity_id=review.id,
            new_value={"reason": "material_amendment_after_review", **applied},
        )
        recalculate_quorum(session, opportunity_id=opportunity_id)
    return changed


def recalculate_quorum(session: Session, *, opportunity_id: int) -> QuorumState:
    """Recompute quorum counts and move an undecided session between review states.

    A decided session (``approved_to_bid`` / ``no_bid``) keeps its status; only
    a reopen changes it. The consolidated review runs once per distinct set of
    completed reviews, so repeated recalculation does not repeat AI/JEV calls
    or approval notifications.
    """
    review = ensure_review_session(session, opportunity_id=opportunity_id)
    assignments = list_assignments(session, opportunity_id)
    completed = [row for row in assignments if row.status == "complete"]
    completed_count = len(completed)
    triggers = _second_review_triggers(session, review, assignments)

    required = 1
    if review.review_policy == "dual":
        required = 2
    elif "reviewer_requested_second_review" in triggers:
        # An explicit reviewer request is honored under every policy.
        required = 2
    elif review.review_policy == "conditional" and triggers:
        required = 2
    required = min(max(required, 1), 2)
    second_required = required > 1
    second_reason = ", ".join(triggers) if triggers else None

    prior_second_required = review.second_review_required
    review.required_review_count = required
    review.completed_review_count = completed_count
    review.second_review_required = second_required
    review.second_review_reason = second_reason
    if review.status not in DECIDED_REVIEW_STATES:
        if completed_count > 0 and review.status in {"ready_for_review", "pending"}:
            review.status = "under_review"
        if completed_count >= required:
            signature = _quorum_signature(completed)
            consolidated = review.ai_consolidated_review or {}
            already_consolidated = (
                consolidated.get("quorum_signature") == signature
                and review.status in {"approval_pending", "review_complete"}
            )
            if not already_consolidated:
                review.status = "review_complete"
                _run_consolidated_review(session, review=review, signature=signature)
        elif review.status != "returned_for_review":
            review.status = "under_review" if assignments else "ready_for_review"
    review.reviewer_summary = _reviewer_summary(
        review=review, assignments=assignments, triggers=triggers
    )
    session.flush()

    if second_required and not prior_second_required:
        for row in assignments:
            notify(
                session,
                user_id=row.user_id,
                opportunity_id=opportunity_id,
                notification_type="second_review_required",
                payload={"reason": second_reason},
            )

    return QuorumState(
        review_policy=review.review_policy,
        required_review_count=required,
        completed_review_count=completed_count,
        second_review_required=second_required,
        second_review_reason=second_reason,
        triggers=triggers,
        quorum_satisfied=completed_count >= required,
        reviewed_by_user_ids=[row.user_id for row in completed],
        pending_user_ids=[row.user_id for row in assignments if row.status != "complete"],
    )


def approval_context(session: Session, *, opportunity_id: int) -> dict[str, Any]:
    review = ensure_review_session(session, opportunity_id=opportunity_id)
    assignments = list_assignments(session, opportunity_id)
    ai_cons = review.ai_consolidated_review or {}
    open_risks = list(ai_cons.get("new_material_risks") or [])
    open_risks.extend(
        finding.description
        for finding in open_findings(session, opportunity_id, blocking_only=False)
        if finding.severity in {"high", "critical"}
    )
    return {
        "review_policy": review.review_policy,
        "required_review_count": review.required_review_count,
        "completed_review_count": review.completed_review_count,
        "who_reviewed": [row.user_id for row in assignments if row.status == "complete"],
        "who_did_not_review": [row.user_id for row in assignments if row.status != "complete"],
        "second_review_triggered": review.second_review_required,
        "second_review_reason": review.second_review_reason,
        "override_used": review.override_used,
        "override_reason": review.override_reason,
        "ai_jev_disagreements": list(ai_cons.get("disagreements_with_ai_package") or []),
        "open_risks": open_risks,
        "decision_package_stale": decision_package_is_stale(session, review),
        "version": review.version,
    }


def finalize_approval(
    session: Session,
    *,
    opportunity_id: int,
    actor: User,
    action: str,
    expected_version: int,
    override_reason: str | None = None,
) -> ReviewSession:
    """Set the final human approval action for a reviewed opportunity.

    The caller must hold ``approve`` and pass the review-session version it
    read. ``approve_to_bid`` requires quorum, or ``override_review`` plus an
    explicit reason, and refuses a decision package built from superseded
    source. ``return_for_review`` reopens completed reviews and undoes the
    downstream proposal approval and submission readiness.
    """
    require_permission(actor, "approve")
    targets = {
        "approve_to_bid": "approved_to_bid",
        "return_for_review": "returned_for_review",
        "no_bid": "no_bid",
    }
    if action not in targets:
        raise ValueError("approval action must be approve_to_bid, return_for_review, or no_bid")
    if expected_version is None:
        raise ReviewWorkflowError("expected_version is required for approval decisions")

    review = _locked_review(session, opportunity_id)
    if action == "approve_to_bid" and review.review_policy == "dual" and any(
        row.user_id == actor.id and row.status == "complete"
        for row in list_assignments(session, opportunity_id)
    ):
        raise ReviewWorkflowError("dual review requires an approver who did not complete a review")
    if review.version != expected_version:
        raise ReviewWorkflowError(
            f"{StaleRecordError(review.version, expected_version)}; reload the review before deciding"
        )
    target = targets[action]
    if review.status == target:
        raise ReviewWorkflowError(f"review session is already {target}")
    try:
        require_transition("review_session", review.status, target)
    except InvalidTransition as exc:
        raise ReviewWorkflowError(str(exc)) from exc
    pursuit_target = _PURSUIT_TARGETS[action]
    pursuit = session.scalar(select(Pursuit).where(Pursuit.opportunity_id == opportunity_id))
    if pursuit is not None and pursuit.stage != pursuit_target and not can_transition("pursuit", pursuit.stage, pursuit_target):
        raise ReviewWorkflowError(
            f"pursuit is {pursuit.stage!r}; the review decision {action} no longer applies"
        )

    quorum = recalculate_quorum(session, opportunity_id=opportunity_id)
    now = datetime.now(UTC)
    settings = get_settings()
    override_fields: dict[str, Any] = {}
    if action == "approve_to_bid":
        package = _decision_package(session, review=review)
        if package is not None and is_stale(
            stamp_of(package.context_manifest), current_source_revision(session, opportunity_id)
        ):
            raise ReviewWorkflowError(
                "the AI decision package was built from a superseded source revision; "
                "regenerate it before approving"
            )
        exception = (
            not quorum.quorum_satisfied
            and _deadline_exception_applies(session, opportunity_id, quorum, settings, now)
        )
        if exception:
            # Roadmap Q5 / ADR-070: one completed review may approve near the
            # deadline, under the owner's explicit setting, with a reason.
            if not override_reason or len(override_reason.strip()) < 10:
                raise ReviewWorkflowError(
                    "the deadline exception allows approval with one completed review, "
                    "but the approver must give a reason of at least 10 characters"
                )
            override_fields = {
                "override_used": True,
                "override_by_user_id": actor.id,
                "override_reason": f"Deadline exception (one review): {override_reason.strip()}",
                "override_at": now,
            }
            record_audit(
                session,
                action_type="review_deadline_exception",
                user_id=actor.id,
                opportunity_id=opportunity_id,
                entity_type="review_sessions",
                entity_id=review.id,
                new_value={
                    "reason": override_reason.strip(),
                    "completed_review_count": quorum.completed_review_count,
                    "required_review_count": quorum.required_review_count,
                    "short_deadline_days": settings.review_short_deadline_days,
                },
            )
        elif not quorum.quorum_satisfied:
            if not settings.review_override_allowed:
                raise ReviewWorkflowError(
                    "review override is disabled by policy; quorum must be satisfied"
                )
            if not override_reason or len(override_reason.strip()) < 10:
                raise ReviewWorkflowError(
                    "approval is blocked until review quorum is satisfied or a valid override reason is provided"
                )
            require_permission(actor, "override_review")
            override_fields = {
                "override_used": True,
                "override_by_user_id": actor.id,
                "override_reason": override_reason.strip(),
                "override_at": now,
            }
            record_audit(
                session,
                action_type="review_quorum_override",
                user_id=actor.id,
                opportunity_id=opportunity_id,
                entity_type="review_sessions",
                entity_id=review.id,
                new_value={"reason": override_reason.strip(), "required_review_count": quorum.required_review_count},
            )

    old = {
        "status": review.status,
        "final_approval_status": review.final_approval_status,
        "version": review.version,
    }
    if action == "return_for_review":
        _reopen_for_return(session, opportunity_id=opportunity_id, actor_id=actor.id)
    _sync_pursuit_stage(
        session, opportunity_id=opportunity_id, action=action, approved_at=now, actor_id=actor.id
    )

    updates: dict[str, Any] = {
        "status": target,
        "final_approval_status": target,
        "approved_by_user_id": actor.id,
        "approved_at": now,
        **override_fields,
    }
    if action == "return_for_review":
        updates["ai_consolidated_review"] = None
    try:
        apply_versioned_update(session, review, review.version, updates)
    except StaleRecordError as exc:
        raise ReviewWorkflowError(str(exc)) from exc

    record_audit(
        session,
        action_type="review_final_approval_set",
        user_id=actor.id,
        opportunity_id=opportunity_id,
        entity_type="review_sessions",
        entity_id=review.id,
        old_value=old,
        new_value={
            "status": review.status,
            "final_approval_status": review.final_approval_status,
            "override_used": review.override_used,
            "version": review.version,
        },
    )
    session.flush()
    if action == "approve_to_bid":
        # The approval and the generation it implies commit together (ADR-062).
        from govcon.workflow.proposal_generation import queue_proposal_generation

        queue_proposal_generation(session, opportunity_id=opportunity_id, actor_user_id=actor.id)
    return review


def review_workspace(
    session: Session, *, opportunity_id: int, user_id: int | None = None
) -> dict[str, Any]:
    review = ensure_review_session(session, opportunity_id=opportunity_id)
    assignments = list_assignments(session, opportunity_id)
    comments = list_comments(session, opportunity_id)
    if user_id is not None:
        assignment = assignment_for_user(
            session, opportunity_id=opportunity_id, user_id=user_id
        )
        if assignment is not None and assignment.status == "assigned":
            start_assignment(session, opportunity_id=opportunity_id, user_id=user_id)
    package = _decision_package(session, review=review)
    return {
        "review_session": {
            "id": review.id,
            "status": review.status,
            "review_policy": review.review_policy,
            "required_review_count": review.required_review_count,
            "completed_review_count": review.completed_review_count,
            "second_review_required": review.second_review_required,
            "second_review_reason": review.second_review_reason,
            "override_used": review.override_used,
            "final_approval_status": review.final_approval_status,
        },
        "decision_package": package.output_json if package else None,
        "assignments": [
            {
                "assignment_id": row.id,
                "user_id": row.user_id,
                "status": row.status,
                "recommendation": row.recommendation,
                "agree_with_ai_assessment": row.agree_with_ai_assessment,
                "second_review_requested": row.second_review_requested,
                "second_review_request_reason": row.second_review_request_reason,
                "completed_at": row.completed_at.isoformat() if row.completed_at else None,
            }
            for row in assignments
        ],
        "comments": [
            {
                "comment_id": row.id,
                "user_id": row.user_id,
                "parent_comment_id": row.parent_comment_id,
                "topic": row.topic,
                "body": row.body,
                "ai_position": row.ai_position,
                "ai_confidence": row.ai_confidence,
                "ai_reason": row.ai_reason,
                "created_at": row.created_at.isoformat(),
            }
            for row in comments
        ],
        "approval_context": approval_context(session, opportunity_id=opportunity_id),
        "ai_consolidated_review": review.ai_consolidated_review,
    }


def _deadline_exception_applies(
    session: Session, opportunity_id: int, quorum: QuorumState, settings: Any, now: datetime
) -> bool:
    """Whether the owner-enabled single-reviewer deadline exception covers this approval.

    It needs at least one completed review and a response deadline that has
    not passed and falls within ``REVIEW_SHORT_DEADLINE_DAYS``.
    """
    from govcon.workflow.app_settings import DEADLINE_EXCEPTION, get_setting

    if quorum.completed_review_count < 1 or not get_setting(session, DEADLINE_EXCEPTION)["enabled"]:
        return False
    opportunity = session.get(Opportunity, opportunity_id)
    deadline = opportunity.response_deadline if opportunity is not None else None
    if deadline is None:
        return False
    return now < deadline <= now + timedelta(days=settings.review_short_deadline_days)


def decision_package_is_stale(session: Session, review: ReviewSession) -> bool:
    """Whether the review's AI decision package was built from a superseded source revision."""
    package = _decision_package(session, review=review)
    if package is None:
        return False
    return is_stale(stamp_of(package.context_manifest), current_source_revision(session, review.opportunity_id))


def _decision_package(session: Session, *, review: ReviewSession) -> AIAnalysis | None:
    analysis = (
        session.get(AIAnalysis, review.ai_decision_package_id)
        if review.ai_decision_package_id
        else None
    )
    if analysis is not None:
        return analysis
    return session.scalar(
        select(AIAnalysis)
        .where(
            AIAnalysis.opportunity_id == review.opportunity_id,
            AIAnalysis.analysis_type == AnalysisType.DECISION_PACKAGE,
        )
        .order_by(desc(AIAnalysis.created_at), desc(AIAnalysis.id))
        .limit(1)
    )


def _reviewer_summary(
    *,
    review: ReviewSession,
    assignments: list[ReviewAssignment],
    triggers: list[str],
) -> dict[str, Any]:
    return {
        "review_policy": review.review_policy,
        "required_review_count": review.required_review_count,
        "completed_review_count": review.completed_review_count,
        "reviewed_by_user_ids": [row.user_id for row in assignments if row.status == "complete"],
        "pending_user_ids": [row.user_id for row in assignments if row.status != "complete"],
        "second_review_required": review.second_review_required,
        "second_review_reason": review.second_review_reason,
        "second_review_triggers": triggers,
        "override_used": review.override_used,
    }


def _second_review_triggers(
    session: Session,
    review: ReviewSession,
    assignments: list[ReviewAssignment],
) -> list[str]:
    settings = get_settings()
    configured = {
        item.strip()
        for item in (settings.review_conditional_triggers or "").split(",")
        if item.strip()
    }

    triggers: list[str] = []
    if any(
        row.second_review_requested for row in assignments
    ):
        triggers.append("reviewer_requested_second_review")

    latest_bid = session.scalar(
        select(BidDecision)
        .where(BidDecision.opportunity_id == review.opportunity_id)
        .order_by(desc(BidDecision.created_at), desc(BidDecision.id))
        .limit(1)
    )
    if (
        "low_jev_confidence" in configured
        and latest_bid
        and latest_bid.recommendation == "review"
    ):
        triggers.append("low_jev_confidence")

    critical_unresolved = int(
        session.scalar(
            select(func.count())
            .where(
                Requirement.opportunity_id == review.opportunity_id,
                Requirement.severity == "critical",
                Requirement.status.in_(
                    ["missing", "needs_review", "unknown", "unreviewed", "stale"]
                ),
            )
        )
        or 0
    )
    if "critical_compliance_risk" in configured and critical_unresolved:
        triggers.append("critical_compliance_risk")

    if "material_amendment" in configured and any(
        finding.finding_type == "review_reopen_required"
        for finding in open_findings(session, review.opportunity_id)
    ):
        triggers.append("material_amendment")

    comments = list_comments(session, review.opportunity_id)
    if "reviewer_ai_disagreement" in configured and any(
        row.ai_position in {"disagree", "insufficient_evidence", "needs_human_review"}
        for row in comments
    ):
        triggers.append("reviewer_ai_disagreement")

    opportunity = session.get(Opportunity, review.opportunity_id)
    if opportunity and opportunity.response_deadline:
        days_remaining = (opportunity.response_deadline - datetime.now(UTC)).total_seconds() / 86400
        if "short_deadline" in configured and days_remaining <= settings.review_short_deadline_days:
            triggers.append("short_deadline")
    if opportunity:
        value = opportunity.estimated_value_max or opportunity.estimated_value_min
        if (
            "high_contract_value" in configured
            and settings.review_high_value_threshold is not None
            and value is not None
            and float(value) >= settings.review_high_value_threshold
        ):
            triggers.append("high_contract_value")
    return sorted(set(triggers))


def _run_consolidated_review(
    session: Session, *, review: ReviewSession, signature: list[list[Any]] | None = None
) -> None:
    assignments = list_assignments(session, review.opportunity_id)
    comments = list_comments(session, review.opportunity_id)
    completed = [row for row in assignments if row.status == "complete"]
    recommendations = [row.recommendation for row in completed if row.recommendation]

    aligned = "single_reviewer"
    if len(set(recommendations)) > 1:
        if {"bid", "no_bid"} <= set(recommendations):
            aligned = "conflicting"
        else:
            aligned = "mixed"
    elif len(completed) > 1:
        aligned = "aligned"

    shared_concerns = sorted(
        {
            row.topic
            for row in comments
            if row.topic and row.ai_position in {"partially_agree", "agree"}
        }
    )
    disagreements = sorted(
        {
            row.topic or row.body[:80]
            for row in comments
            if row.ai_position in {"disagree", "needs_human_review"}
        }
    )
    open_questions = sorted(
        {
            item
            for row in comments
            for item in (row.ai_missing_information or [])
            if isinstance(item, str)
        }
    )
    review_mode = (
        "override-driven"
        if review.override_used
        else review.review_policy
    )
    consolidated = {
        "reviewer_alignment": aligned,
        "review_mode": review_mode,
        "shared_concerns": shared_concerns,
        "disagreements": disagreements,
        "disagreements_with_ai_package": disagreements,
        "new_material_risks": disagreements,
        "resolved_issues": [],
        "open_questions": open_questions,
        "evidence_needed_before_approval": open_questions,
        "prior_analysis_stale": any(
            finding.finding_type == "review_reopen_required"
            for finding in open_findings(session, review.opportunity_id)
        ),
        "summary": " ".join(
            [
                f"{len(completed)} reviewer(s) completed.",
                f"Alignment={aligned}.",
            ]
        ),
        "human_approval_required": True,
    }

    package = _decision_package(session, review=review)
    try:
        ai_output = run_consolidated_review_prompt(
            session,
            opportunity_id=review.opportunity_id,
            review_summary=consolidated,
            package=package.output_json if package else {},
            assignments=assignments,
            comments=comments,
        )
    except Exception:
        ai_output = consolidated

    state = build_decision_state(session, review.opportunity_id)
    state["review"] = {
        "review_policy": review.review_policy,
        "required_review_count": review.required_review_count,
        "completed_review_count": review.completed_review_count,
    }
    state["collaboration"] = {
        "reviewer_recommendations": recommendations,
        "ai_consolidated_review": ai_output,
    }
    jev = run_decision_bundle(
        session,
        opportunity_id=review.opportunity_id,
        bundle_name="collaborative_review_synthesis",
        state=state,
    )
    ai_output["jev_final_recommendation"] = jev.result.get("recommendation")
    ai_output["approval_gate_status"] = jev.result.get("approval_gate_status")
    ai_output["decision_run_id"] = jev.run.id
    ai_output["quorum_signature"] = signature
    review.ai_consolidated_review = ai_output
    review.status = "approval_pending"
    session.flush()
    for user in session.scalars(
        select(User).where(User.is_active.is_(True), User.role.in_(["owner", "approver"]))
    ).all():
        notify(
            session,
            user_id=user.id,
            opportunity_id=review.opportunity_id,
            notification_type="approval_pending",
            payload={"review_session_id": review.id},
        )
    notify(
        session,
        user_id=completed[0].user_id if completed else assignments[0].user_id,
        opportunity_id=review.opportunity_id,
        notification_type="review_quorum_satisfied",
        payload={"review_session_id": review.id},
    )


def run_consolidated_review_prompt(
    session: Session,
    *,
    opportunity_id: int,
    review_summary: dict[str, Any],
    package: dict[str, Any],
    assignments: list[ReviewAssignment],
    comments: list[Any],
) -> dict[str, Any]:
    from govcon.ai.structured import StructuredCallError, run_structured_prompt

    recommendations = [
        {
            "user_id": row.user_id,
            "recommendation": row.recommendation,
            "agree_with_ai_assessment": row.agree_with_ai_assessment,
            "second_review_requested": row.second_review_requested,
            "second_review_request_reason": row.second_review_request_reason,
            "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        }
        for row in assignments
    ]
    comments_json = [
        {
            "comment_id": row.id,
            "user_id": row.user_id,
            "topic": row.topic,
            "body": row.body,
            "ai_position": row.ai_position,
            "ai_confidence": row.ai_confidence,
            "ai_reason": row.ai_reason,
            "ai_missing_information": row.ai_missing_information or [],
        }
        for row in comments
    ]
    try:
        result = run_structured_prompt(
            session,
            classification=DataClassification.PROPRIETARY,
            opportunity_id=opportunity_id,
            prompt_name="consolidated_review",
            analysis_type=AnalysisType.CONSOLIDATED_REVIEW,
            variables={
                "AI_DECISION_PACKAGE_JSON": package,
                "REVIEWER_RECOMMENDATIONS_JSON": recommendations,
                "REVIEWER_COMMENTS_JSON": comments_json,
                "AI_COMMENT_VALIDATIONS_JSON": comments_json,
                "CURRENT_STATE_JSON": {
                    "open_findings": [
                        {
                            "type": finding.finding_type,
                            "severity": finding.severity,
                            "description": finding.description,
                        }
                        for finding in open_findings(session, opportunity_id)
                    ]
                },
                "REVIEW_QUORUM_STATE_JSON": review_summary,
            },
            context_manifest={
                "opportunity_id": opportunity_id,
                "reviewer_alignment": review_summary["reviewer_alignment"],
                "review_mode": review_summary["review_mode"],
            },
        )
    except StructuredCallError as exc:
        raise ReviewWorkflowError(
            f"consolidated review AI failed ({exc.reason}): {exc.detail}"
        ) from exc
    return result.output.model_dump(mode="json")


def _sync_pursuit_stage(
    session: Session,
    *,
    opportunity_id: int,
    action: str,
    approved_at: datetime,
    actor_id: int | None = None,
) -> None:
    target = _PURSUIT_TARGETS[action]
    lock_opportunity(session, opportunity_id)
    pursuit = lock_one(session, select(Pursuit).where(Pursuit.opportunity_id == opportunity_id))
    if pursuit is None:
        from govcon.workflow.pursuits import create_or_get_pursuit

        actor = session.get(User, actor_id) if actor_id else None
        pursuit, _ = create_or_get_pursuit(session, opportunity_id=opportunity_id, actor=actor, origin="approval")
        pursuit = lock_one(session, select(Pursuit).where(Pursuit.id == pursuit.id))
    old_stage = pursuit.stage
    if old_stage != target:
        try:
            require_transition("pursuit", old_stage, target)
        except InvalidTransition as exc:
            raise ReviewWorkflowError(f"{exc}; the pursuit is past the approval stage") from exc
        pursuit.stage = target
        pursuit.version = (pursuit.version or 1) + 1
    if action == "approve_to_bid":
        pursuit.approved_to_bid_at = approved_at
    else:
        pursuit.approved_to_bid_at = None
    session.flush()
    if old_stage != target:
        record_audit(
            session,
            action_type="pursuit_stage_changed",
            user_id=actor_id,
            opportunity_id=opportunity_id,
            entity_type="pursuits",
            entity_id=pursuit.id,
            old_value={"stage": old_stage},
            new_value={"stage": target, "reason": f"review decision {action}"},
        )


# A revoked/returned bid approval sends the pursuit back to evaluating.
_PURSUIT_TARGETS = {"approve_to_bid": "bid_approved", "no_bid": "no_bid", "return_for_review": "evaluating"}


def _reopen_for_return(session: Session, *, opportunity_id: int, actor_id: int) -> None:
    """``return_for_review``: reviewers must look again and downstream approvals lapse."""
    for assignment in list_assignments(session, opportunity_id):
        if assignment.status == "complete":
            assignment.status = "reopened"
            assignment.reopened_at = datetime.now(UTC)
            assignment.completed_at = None
            notify(
                session,
                user_id=assignment.user_id,
                opportunity_id=opportunity_id,
                notification_type="review_assigned",
                payload={"reason": "returned_for_review"},
            )
    invalidate_proposal_approval(session, opportunity_id, reason="returned_for_review", actor_id=actor_id)
    invalidate_submission_readiness(session, opportunity_id, reason="returned_for_review", actor_id=actor_id)
    session.flush()


def _locked_review(session: Session, opportunity_id: int) -> ReviewSession:
    lock_opportunity(session, opportunity_id)
    ensure_review_session(session, opportunity_id=opportunity_id)
    review = lock_one(session, select(ReviewSession).where(ReviewSession.opportunity_id == opportunity_id))
    assert review is not None
    return review


def _approvers(session: Session) -> list[User]:
    return list(
        session.scalars(
            select(User).where(User.is_active.is_(True), User.role.in_(["owner", "approver"]))
        ).all()
    )


def _quorum_signature(completed: list[ReviewAssignment]) -> list[list[Any]]:
    return sorted(
        [row.id, row.completed_at.isoformat() if row.completed_at else None, row.recommendation]
        for row in completed
    )


