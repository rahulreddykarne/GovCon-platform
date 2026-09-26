"""Review assignment lifecycle helpers for collaborative review."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.audit import record_audit
from govcon.collaboration.notifications import notify
from govcon.models import ReviewAssignment, User


def list_assignments(session: Session, opportunity_id: int) -> list[ReviewAssignment]:
    return list(
        session.scalars(
            select(ReviewAssignment)
            .where(ReviewAssignment.opportunity_id == opportunity_id)
            .order_by(ReviewAssignment.id)
        ).all()
    )


def assignment_for_user(
    session: Session, *, opportunity_id: int, user_id: int
) -> ReviewAssignment | None:
    return session.scalar(
        select(ReviewAssignment).where(
            ReviewAssignment.opportunity_id == opportunity_id,
            ReviewAssignment.user_id == user_id,
        )
    )


def assign_reviewer(
    session: Session,
    *,
    opportunity_id: int,
    user_id: int,
    assignment_role: str = "reviewer",
    actor_user_id: int | None = None,
) -> ReviewAssignment:
    user = session.get(User, user_id)
    if user is None or not user.is_active:
        raise ValueError("review assignments require an active user")

    existing = assignment_for_user(
        session, opportunity_id=opportunity_id, user_id=user_id
    )
    old_value = None
    if existing is None:
        existing = ReviewAssignment(
            opportunity_id=opportunity_id,
            user_id=user_id,
            assignment_role=assignment_role,
            status="assigned",
            assigned_at=datetime.now(UTC),
        )
        session.add(existing)
    else:
        old_value = {
            "status": existing.status,
            "assignment_role": existing.assignment_role,
        }
        existing.assignment_role = assignment_role
        if existing.status not in {"assigned", "in_progress"}:
            existing.status = "assigned"
            existing.started_at = None
            existing.completed_at = None
    session.flush()

    record_audit(
        session,
        action_type="review_assignment_upserted",
        user_id=actor_user_id,
        opportunity_id=opportunity_id,
        entity_type="review_assignments",
        entity_id=existing.id,
        old_value=old_value,
        new_value={
            "user_id": user_id,
            "status": existing.status,
            "assignment_role": existing.assignment_role,
        },
    )
    notify(
        session,
        user_id=user_id,
        notification_type="review_assigned",
        opportunity_id=opportunity_id,
        payload={"assignment_id": existing.id},
    )
    return existing


def start_assignment(
    session: Session,
    *,
    opportunity_id: int,
    user_id: int,
) -> ReviewAssignment:
    assignment = assignment_for_user(
        session, opportunity_id=opportunity_id, user_id=user_id
    )
    if assignment is None:
        raise ValueError("review assignment not found")
    if assignment.status in {"assigned", "reopened"}:
        assignment.status = "in_progress"
    if assignment.started_at is None:
        assignment.started_at = datetime.now(UTC)
    session.flush()
    return assignment


def reassign_reviewer(
    session: Session,
    *,
    opportunity_id: int,
    from_user_id: int,
    to_user_id: int,
    actor_user_id: int | None = None,
    reason: str | None = None,
) -> tuple[ReviewAssignment | None, ReviewAssignment]:
    old_assignment = assignment_for_user(
        session, opportunity_id=opportunity_id, user_id=from_user_id
    )
    if old_assignment is not None and old_assignment.status != "reopened":
        old_assignment.status = "reopened"
        old_assignment.reopened_at = datetime.now(UTC)
    new_assignment = assign_reviewer(
        session,
        opportunity_id=opportunity_id,
        user_id=to_user_id,
        actor_user_id=actor_user_id,
    )
    record_audit(
        session,
        action_type="review_reassigned",
        user_id=actor_user_id,
        opportunity_id=opportunity_id,
        entity_type="review_assignments",
        entity_id=new_assignment.id,
        old_value={"from_user_id": from_user_id},
        new_value={"to_user_id": to_user_id, "reason": reason},
    )
    notify(
        session,
        user_id=to_user_id,
        opportunity_id=opportunity_id,
        notification_type="review_reassigned",
        payload={"from_user_id": from_user_id, "reason": reason},
    )
    return old_assignment, new_assignment
