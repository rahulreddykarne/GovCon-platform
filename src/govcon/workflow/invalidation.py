"""Invalidate derived workflow state when the source changes or a decision is reversed.

One workflow handles a material source change end to end:

1. ingest emits a ``material_source_change`` event (``ingest/snapshots.py``);
2. ``process_pending_source_changes`` first calls ``apply_source_change``,
   which reopens the review, invalidates the proposal approval and pre-flight
   readiness, and moves pursuit and submission back;
3. it then fetches new attachments, rebuilds the inventory and reruns
   compliance. The event is marked handled only after both steps succeed; a
   failed attempt is recorded and retried with backoff.

Each change is version-bumped and audited so a stale web form or MCP call
cannot commit over it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.audit import record_audit
from govcon.collaboration.notifications import notify
from govcon.models import (
    ComplianceFinding,
    Opportunity,
    OpportunityEvent,
    Proposal,
    Pursuit,
    ReviewAssignment,
    ReviewSession,
    Submission,
)
from govcon.workflow.source_events import (
    MATERIAL_SOURCE_CHANGE_EVENT,
    MATERIAL_SOURCE_CHANGE_FAILED_EVENT,
    MATERIAL_SOURCE_CHANGE_HANDLED_EVENT,
)
from govcon.workflow.transitions import can_transition

logger = logging.getLogger("govcon.workflow.invalidation")

DECIDED_REVIEW_STATES = frozenset({"approved_to_bid", "no_bid"})
_REOPENABLE_REVIEW_STATES = frozenset({"review_complete", "approval_pending", "approved_to_bid", "no_bid"})
_POST_SUBMISSION_STAGES = frozenset({"submitted", "won", "lost"})
# Backoff after a failed source-change attempt: 5 minutes, doubling, at most 6 hours.
RETRY_BASE_SECONDS = 300
RETRY_MAX_SECONDS = 6 * 3600


def _bump(row: Any) -> None:
    row.version = (row.version or 1) + 1


def reopen_review(
    session: Session,
    opportunity_id: int,
    *,
    reason: str,
    actor_id: int | None = None,
) -> bool:
    """Reopen completed assignments and any decided/pending-approval session state.

    Works for override-approved sessions with no completed assignments too.
    Returns True when anything changed.
    """
    lock_opportunity(session, opportunity_id)
    review = lock_one(session, select(ReviewSession).where(ReviewSession.opportunity_id == opportunity_id))
    if review is None:
        return False
    now = datetime.now(UTC)
    assignments = list(
        session.scalars(
            select(ReviewAssignment)
            .where(ReviewAssignment.opportunity_id == opportunity_id)
            .order_by(ReviewAssignment.id)
        ).all()
    )
    reopened_ids: list[int] = []
    for assignment in assignments:
        if assignment.status == "complete":
            assignment.status = "reopened"
            assignment.reopened_at = now
            assignment.completed_at = None
            reopened_ids.append(assignment.id)
            notify(
                session,
                user_id=assignment.user_id,
                opportunity_id=opportunity_id,
                notification_type="material_amendment_after_review"
                if reason.startswith("material")
                else "review_assigned",
                payload={"review_session_id": review.id, "reason": reason},
            )
    status_changed = review.status in _REOPENABLE_REVIEW_STATES
    if not reopened_ids and not status_changed:
        return False

    old = {
        "status": review.status,
        "final_approval_status": review.final_approval_status,
        "approved_by_user_id": review.approved_by_user_id,
        "override_used": review.override_used,
        "override_reason": review.override_reason,
        "version": review.version,
    }
    target = "under_review" if assignments else "ready_for_review"
    if can_transition("review_session", review.status, target):
        review.status = target
    review.final_approval_status = None
    review.approved_by_user_id = None
    review.approved_at = None
    # A prior override applied to the prior decision only.
    review.override_used = False
    review.override_by_user_id = None
    review.override_reason = None
    review.override_at = None
    review.ai_consolidated_review = None
    review.completed_review_count = 0
    review.second_review_required = True
    review.second_review_reason = reason
    _bump(review)
    session.flush()
    record_audit(
        session,
        action_type="review_reopened",
        user_id=actor_id,
        opportunity_id=opportunity_id,
        entity_type="review_sessions",
        entity_id=review.id,
        old_value=old,
        new_value={"status": review.status, "reason": reason, "reopened_assignment_ids": reopened_ids},
    )
    return True


def invalidate_proposal_approval(
    session: Session,
    opportunity_id: int,
    *,
    reason: str,
    actor_id: int | None = None,
    target_status: str = "returned_for_fix",
) -> bool:
    """Move a final-approved proposal back so it must be reviewed and approved again."""
    lock_opportunity(session, opportunity_id)
    proposal = lock_one(session, select(Proposal).where(Proposal.opportunity_id == opportunity_id))
    if proposal is None or proposal.status != "final_approved":
        return False
    old = {
        "status": proposal.status,
        "approved_version_id": proposal.approved_version_id,
        "final_approved_by_user_id": proposal.final_approved_by_user_id,
        "version": proposal.version,
    }
    proposal.status = target_status
    proposal.approved_version_id = None
    proposal.final_approved_by_user_id = None
    proposal.final_approved_at = None
    _bump(proposal)
    session.flush()
    record_audit(
        session,
        action_type="proposal_approval_invalidated",
        user_id=actor_id,
        opportunity_id=opportunity_id,
        entity_type="proposals",
        entity_id=proposal.id,
        old_value=old,
        new_value={"status": proposal.status, "reason": reason},
    )
    return True


def invalidate_submission_readiness(
    session: Session,
    opportunity_id: int,
    *,
    reason: str,
    actor_id: int | None = None,
) -> bool:
    """Undo ``ready``: submission back to ``preparing``; pursuit out of ``ready_to_submit``."""
    changed = False
    lock_opportunity(session, opportunity_id)
    submission = lock_one(
        session,
        select(Submission)
        .where(Submission.opportunity_id == opportunity_id)
        .order_by(Submission.id.desc())
        .limit(1),
    )
    if submission is not None and (
        submission.status == "ready" or submission.readiness_status in {"ready", "needs_review"}
    ):
        old = {"status": submission.status, "readiness_status": submission.readiness_status, "version": submission.version}
        if submission.status == "ready":
            submission.status = "preparing"
        submission.readiness_status = "not_ready"
        _bump(submission)
        changed = True
        record_audit(
            session,
            action_type="submission_readiness_invalidated",
            user_id=actor_id,
            opportunity_id=opportunity_id,
            entity_type="submissions",
            entity_id=submission.id,
            old_value=old,
            new_value={"status": submission.status, "readiness_status": "not_ready", "reason": reason},
        )
    pursuit = _locked_pursuit(session, opportunity_id)
    if pursuit is not None and pursuit.stage == "ready_to_submit":
        _move_pursuit(session, pursuit, "drafting", reason=reason, actor_id=actor_id)
        changed = True
    session.flush()
    return changed


def rollback_pursuit_for_review(
    session: Session,
    opportunity_id: int,
    *,
    reason: str,
    actor_id: int | None = None,
) -> bool:
    """Revoke the bid approval on the pursuit: back to ``evaluating``.

    A pursuit already past submission is not moved; a finding flags the change.
    """
    lock_opportunity(session, opportunity_id)
    pursuit = _locked_pursuit(session, opportunity_id)
    if pursuit is None:
        return False
    if pursuit.stage in _POST_SUBMISSION_STAGES:
        _flag_post_submission_change(session, opportunity_id, pursuit, reason=reason)
        return False
    if pursuit.stage in {"evaluating", "cancelled"} or not can_transition("pursuit", pursuit.stage, "evaluating"):
        return False
    pursuit.approved_to_bid_at = None
    _move_pursuit(session, pursuit, "evaluating", reason=reason, actor_id=actor_id)
    return True


def apply_source_change(
    session: Session,
    opportunity_id: int,
    *,
    level: str,
    reason: str,
    actor_id: int | None = None,
) -> dict[str, bool]:
    """Invalidate every derived approval that depended on the previous source.

    ``level="material"`` reopens review and approvals; ``"readiness"`` only
    invalidates submission readiness (e.g. a deadline change).
    """
    result = {"review_reopened": False, "proposal_invalidated": False, "readiness_invalidated": False, "pursuit_rolled_back": False}
    if level == "material":
        result["review_reopened"] = reopen_review(session, opportunity_id, reason=reason, actor_id=actor_id)
        result["proposal_invalidated"] = invalidate_proposal_approval(session, opportunity_id, reason=reason, actor_id=actor_id)
        result["readiness_invalidated"] = invalidate_submission_readiness(session, opportunity_id, reason=reason, actor_id=actor_id)
        result["pursuit_rolled_back"] = rollback_pursuit_for_review(session, opportunity_id, reason=reason, actor_id=actor_id)
    elif level == "readiness":
        result["readiness_invalidated"] = invalidate_submission_readiness(session, opportunity_id, reason=reason, actor_id=actor_id)
    else:
        raise ValueError(f"unknown source-change level: {level!r}")
    return result


def _handled_event_ids() -> Any:
    event_id = OpportunityEvent.old_value["event_id"].as_integer()
    return (
        select(event_id)
        .where(
            OpportunityEvent.event_type == MATERIAL_SOURCE_CHANGE_HANDLED_EVENT,
            # NOT IN with a NULL would hide every pending event.
            event_id.is_not(None),
        )
        .scalar_subquery()
    )


def pending_source_change_events(
    session: Session,
    *,
    limit: int = 200,
    opportunity_ids: set[int] | None = None,
    exclude_ids: set[int] | None = None,
) -> list[OpportunityEvent]:
    """Unhandled ``material_source_change`` events, oldest first.

    ``exclude_ids`` skips events still waiting for their retry time.
    """
    query = select(OpportunityEvent).where(
        OpportunityEvent.event_type == MATERIAL_SOURCE_CHANGE_EVENT,
        OpportunityEvent.id.not_in(_handled_event_ids()),
    )
    if opportunity_ids is not None:
        query = query.where(OpportunityEvent.opportunity_id.in_(sorted(opportunity_ids) or [-1]))
    if exclude_ids:
        query = query.where(OpportunityEvent.id.not_in(sorted(exclude_ids)))
    return list(session.scalars(query.order_by(OpportunityEvent.id).limit(limit)).all())


def retry_delay(attempts: int) -> timedelta:
    """Backoff after ``attempts`` failed attempts: doubling, capped."""
    if attempts <= 0:
        return timedelta(0)
    seconds = RETRY_BASE_SECONDS * 2 ** min(attempts - 1, 16)
    return timedelta(seconds=min(seconds, RETRY_MAX_SECONDS))


def failed_attempts(
    session: Session, *, opportunity_ids: set[int] | None = None
) -> dict[int, tuple[int, datetime | None]]:
    """``{event_id: (failed attempts, last failure time)}`` for still-unhandled events."""
    event_id = OpportunityEvent.old_value["event_id"].as_integer()
    query = select(event_id, OpportunityEvent.detected_at).where(
        OpportunityEvent.event_type == MATERIAL_SOURCE_CHANGE_FAILED_EVENT,
        event_id.is_not(None),
        event_id.not_in(_handled_event_ids()),
    )
    if opportunity_ids is not None:
        query = query.where(OpportunityEvent.opportunity_id.in_(sorted(opportunity_ids) or [-1]))
    attempts: dict[int, tuple[int, datetime | None]] = {}
    for failed_id, detected_at in session.execute(query).all():
        count, last = attempts.get(int(failed_id), (0, None))
        if last is None or (detected_at is not None and detected_at > last):
            last = detected_at
        attempts[int(failed_id)] = (count + 1, last)
    return attempts


def retry_not_due(attempts: dict[int, tuple[int, datetime | None]], now: datetime) -> set[int]:
    """Event ids whose last failure is more recent than their backoff allows."""
    waiting: set[int] = set()
    for event_id, (count, last) in attempts.items():
        if last is None:
            continue
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        if now < last + retry_delay(count):
            waiting.add(event_id)
    return waiting


def _has_workflow_state(session: Session, opportunity_id: int) -> bool:
    return bool(
        session.scalar(select(Pursuit.id).where(Pursuit.opportunity_id == opportunity_id))
        or session.scalar(select(ReviewSession.id).where(ReviewSession.opportunity_id == opportunity_id))
    )


def process_pending_source_changes(
    session: Session,
    *,
    settings: Any = None,
    fetch_attachments: bool = True,
    use_ai: bool | None = None,
    client: Any = None,
    opportunity_ids: set[int] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run the source-change workflow for every unhandled ingest event.

    ``opportunity_ids`` limits the run to those opportunities.

    Dependent approvals are invalidated first, in their own savepoint, so a
    later failure cannot leave an approval based on the old source usable.
    Attachments and compliance are then revalidated. An event is marked
    handled only when both steps succeed; otherwise a
    ``material_source_change_failed`` attempt is recorded and the event is
    retried with exponential backoff (invalidation is idempotent).

    Opportunities without a pursuit or review session have no derived approvals
    to invalidate; their events are marked handled. Cached analyses are
    protected separately by source-revision stamps.
    """
    from govcon.config import get_settings

    settings = settings or get_settings()
    if use_ai is None:
        from govcon.ai.providers import provider_available
        use_ai = provider_available(settings)
    now = now or datetime.now(UTC)
    summary: dict[str, Any] = {"events": 0, "opportunities": 0, "invalidated": 0, "deferred": 0, "errors": []}
    attempts = failed_attempts(session, opportunity_ids=opportunity_ids)
    waiting = retry_not_due(attempts, now)
    summary["deferred"] = len(waiting)
    events = pending_source_change_events(session, opportunity_ids=opportunity_ids, exclude_ids=waiting)
    by_opportunity: dict[int, list[OpportunityEvent]] = {}
    for event in events:
        by_opportunity.setdefault(event.opportunity_id, []).append(event)
    if waiting and by_opportunity:
        # An opportunity processed now covers its waiting events too; retrying
        # them later would reopen approvals given after this revalidation.
        for event in session.scalars(
            select(OpportunityEvent)
            .where(
                OpportunityEvent.id.in_(sorted(waiting)),
                OpportunityEvent.opportunity_id.in_(sorted(by_opportunity)),
            )
            .order_by(OpportunityEvent.id)
        ).all():
            by_opportunity[event.opportunity_id].append(event)
            summary["deferred"] -= 1
            events.append(event)
        for rows in by_opportunity.values():
            rows.sort(key=lambda row: row.id)
    summary["events"] = len(events)
    # Opportunity id order: locks held to commit are always taken in one order.
    for opportunity_id, rows in sorted(by_opportunity.items()):
        levels = {((row.new_value or {}).get("value") or {}).get("level") for row in rows}
        level = "material" if "material" in levels else "readiness"
        reason = "material_amendment_after_review" if level == "material" else "source_deadline_changed"
        outcome: dict[str, Any] = {"level": level, "workflow_state": False}
        step = "invalidate"
        try:
            # Committed independently of revalidation: the old approval must not
            # stay usable while the new source is still being processed.
            with session.begin_nested():
                lock_opportunity(session, opportunity_id)
                if _has_workflow_state(session, opportunity_id):
                    outcome["workflow_state"] = True
                    outcome["applied"] = apply_source_change(session, opportunity_id, level=level, reason=reason)
            if outcome["workflow_state"]:
                summary["invalidated"] += int(any(outcome["applied"].values()))
                step = "revalidate"
                with session.begin_nested():
                    outcome.update(
                        _revalidate_sources(
                            session,
                            opportunity_id,
                            level=level,
                            settings=settings,
                            fetch_attachments=fetch_attachments,
                            use_ai=use_ai,
                            client=client,
                        )
                    )
        except Exception as exc:  # one opportunity must not stop the rest
            logger.exception("source-change workflow failed for opportunity %s (%s)", opportunity_id, step)
            error = f"{type(exc).__name__}: {exc}"
            summary["errors"].append(f"opportunity {opportunity_id}: {error}")
            for row in rows:
                attempt = attempts.get(row.id, (0, None))[0] + 1
                session.add(
                    OpportunityEvent(
                        opportunity_id=opportunity_id,
                        event_type=MATERIAL_SOURCE_CHANGE_FAILED_EVENT,
                        field_name=None,
                        old_value={"event_id": row.id},
                        new_value={
                            "value": {
                                **outcome,
                                "step": step,
                                "error": error[:1000],
                                "attempt": attempt,
                                "retry_after": (now + retry_delay(attempt)).isoformat(),
                            }
                        },
                        snapshot_id=row.snapshot_id,
                    )
                )
            session.flush()
            continue
        summary["opportunities"] += 1
        for row in rows:
            session.add(
                OpportunityEvent(
                    opportunity_id=opportunity_id,
                    event_type=MATERIAL_SOURCE_CHANGE_HANDLED_EVENT,
                    field_name=None,
                    old_value={"event_id": row.id},
                    new_value={"value": outcome},
                    snapshot_id=row.snapshot_id,
                )
            )
        session.flush()
    return summary


def _revalidate_sources(
    session: Session,
    opportunity_id: int,
    *,
    level: str,
    settings: Any,
    fetch_attachments: bool,
    use_ai: bool,
    client: Any,
) -> dict[str, Any]:
    """Fetch new attachments and rerun compliance so the matrix reflects the new source.

    Both levels then regenerate the submission instructions (recipient,
    deadline, required files) so pre-flight never checks superseded values.
    """
    out: dict[str, Any] = {}
    if level == "material":
        from govcon.compliance.pipeline import run_compliance_pipeline
        from govcon.enrich.attachments import download_attachments

        opportunity = session.get(Opportunity, opportunity_id)
        if fetch_attachments and opportunity is not None:
            files = download_attachments(session, opportunity, settings=settings, client=client)
            out["attachments"] = len(files)
        compliance = run_compliance_pipeline(session, opportunity_id, use_ai=use_ai, settings=settings)
        out["compliance_status"] = compliance.get("status")
    submission = session.scalar(
        select(Submission).where(Submission.opportunity_id == opportunity_id).order_by(Submission.id.desc()).limit(1)
    )
    if submission is not None and submission.status not in {"submitted", "confirmed", "withdrawn"}:
        from govcon.submissions.service import generate_submission_package

        generate_submission_package(session, opportunity_id=opportunity_id, settings=settings)
        out["submission_instructions_refreshed"] = True
    return out


def lock_opportunity(session: Session, opportunity_id: int) -> Opportunity | None:
    """Take the opportunity's workflow lock. Must come before any child-row lock.

    Locking convention for every workflow write on one opportunity (review,
    proposal, pursuit, submission, package): lock the ``opportunities`` row
    first, then child rows. Concurrent workflows on one opportunity then
    serialize on this row instead of locking children in opposing orders
    (e.g. return-for-fix vs. submission confirmation). Re-taking it in the
    same transaction is a no-op.

    ``FOR NO KEY UPDATE`` still conflicts with other workflow locks but not
    with the key-share lock that inserting a child row (an audit event, a
    notification) takes on its parent, so those inserts never wait on it.
    """
    session.flush()
    return session.scalar(
        select(Opportunity)
        .where(Opportunity.id == opportunity_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )


def lock_one(session: Session, statement: Any) -> Any:
    """``SELECT ... FOR UPDATE`` one row and refresh it from the database.

    Pending changes are flushed first so ``populate_existing`` cannot discard
    them (application sessions run with ``autoflush=False``).
    """
    session.flush()
    return session.scalar(statement.with_for_update().execution_options(populate_existing=True))


def _locked_pursuit(session: Session, opportunity_id: int) -> Pursuit | None:
    return lock_one(session, select(Pursuit).where(Pursuit.opportunity_id == opportunity_id))


def _move_pursuit(session: Session, pursuit: Pursuit, target: str, *, reason: str, actor_id: int | None) -> None:
    old = pursuit.stage
    pursuit.stage = target
    _bump(pursuit)
    session.flush()
    record_audit(
        session,
        action_type="pursuit_stage_changed",
        user_id=actor_id,
        opportunity_id=pursuit.opportunity_id,
        entity_type="pursuits",
        entity_id=pursuit.id,
        old_value={"stage": old},
        new_value={"stage": target, "reason": reason},
    )


def _flag_post_submission_change(session: Session, opportunity_id: int, pursuit: Pursuit, *, reason: str) -> None:
    existing = session.scalar(
        select(ComplianceFinding).where(
            ComplianceFinding.opportunity_id == opportunity_id,
            ComplianceFinding.finding_type == "source_change_after_submission",
            ComplianceFinding.status == "open",
        )
    )
    if existing is not None:
        return
    session.add(
        ComplianceFinding(
            opportunity_id=opportunity_id,
            finding_type="source_change_after_submission",
            severity="high",
            description=(
                f"The solicitation changed materially after the pursuit reached {pursuit.stage!r} ({reason}). "
                "Check whether the amendment must be acknowledged or the offer revised."
            ),
            detected_by="source_change_workflow",
            detector_version="source_change.v1",
            blocks_submission=False,
            certainty="confirmed",
        )
    )
    session.flush()
