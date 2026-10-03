"""Review reminders, overdue escalation and deadline escalation (roadmap gap 8, ADR-070).

Runs in the midday scheduler chain. Each notification is in-app and, when
email is enabled, delivered by a durable email task:

- **reminder**: an open review assignment older than ``REVIEW_REMINDER_HOURS``
  reminds its reviewer, at most once a day;
- **overdue**: an assignment older than ``REVIEW_OVERDUE_HOURS`` is escalated
  once to approvers and owners;
- **deadline**: an undecided review whose response deadline is within
  ``REVIEW_ESCALATE_DAYS_BEFORE_DEADLINE`` days is escalated to approvers and
  owners, at most once a day per opportunity.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.collaboration.notifications import notify
from govcon.config import Settings, get_settings
from govcon.models import Notification, Opportunity, ReviewAssignment, ReviewSession, User

OPEN_ASSIGNMENTS = ("assigned", "in_progress", "reopened")
DECIDED_REVIEWS = ("approved_to_bid", "no_bid")


@dataclass
class EscalationCounts:
    reminders: int = 0
    overdue: int = 0
    deadline: int = 0


def _approvers(session: Session) -> list[int]:
    return list(session.scalars(select(User.id).where(User.is_active.is_(True), User.role.in_(("owner", "approver")))))


def run_review_escalations(session: Session, *, settings: Settings | None = None,
                           now: datetime | None = None) -> EscalationCounts:
    settings = settings or get_settings()
    now = now or datetime.now(UTC)
    counts = EscalationCounts()
    approvers = _approvers(session)
    open_reviews = (
        select(ReviewAssignment, Opportunity)
        .join(Opportunity, Opportunity.id == ReviewAssignment.opportunity_id)
        .join(ReviewSession, ReviewSession.opportunity_id == ReviewAssignment.opportunity_id)
        .where(ReviewAssignment.status.in_(OPEN_ASSIGNMENTS), ReviewSession.status.not_in(DECIDED_REVIEWS))
    )
    for assignment, opportunity in session.execute(open_reviews):
        age = now - (assignment.reopened_at or assignment.assigned_at)
        payload = {"title": opportunity.title, "assigned_hours_ago": int(age.total_seconds() // 3600)}
        if age >= timedelta(hours=settings.review_reminder_hours) and (
            assignment.last_reminded_at is None or now - assignment.last_reminded_at >= timedelta(hours=24)
        ):
            notify(session, user_id=assignment.user_id, notification_type="review_reminder",
                   opportunity_id=opportunity.id, payload=payload, settings=settings)
            assignment.last_reminded_at = now
            counts.reminders += 1
        if age >= timedelta(hours=settings.review_overdue_hours) and assignment.escalated_at is None:
            for user_id in approvers:
                notify(session, user_id=user_id, notification_type="review_overdue_escalation",
                       opportunity_id=opportunity.id, payload={**payload, "reviewer_user_id": assignment.user_id},
                       settings=settings)
            assignment.escalated_at = now
            counts.overdue += 1

    horizon = now + timedelta(days=settings.review_escalate_days_before_deadline)
    undecided = session.execute(
        select(ReviewSession, Opportunity)
        .join(Opportunity, Opportunity.id == ReviewSession.opportunity_id)
        .where(ReviewSession.status.not_in(DECIDED_REVIEWS), Opportunity.response_deadline.is_not(None),
               Opportunity.response_deadline > now, Opportunity.response_deadline <= horizon)
    )
    for review, opportunity in undecided:
        recent = session.scalar(
            select(Notification.id).where(
                Notification.opportunity_id == opportunity.id,
                Notification.notification_type == "deadline_escalation",
                Notification.created_at > now - timedelta(hours=24),
            ).limit(1)
        )
        if recent is not None:
            continue
        hours_left = int((opportunity.response_deadline - now).total_seconds() // 3600)
        for user_id in approvers:
            notify(session, user_id=user_id, notification_type="deadline_escalation", opportunity_id=opportunity.id,
                   payload={"title": opportunity.title, "hours_left": hours_left, "review_status": review.status},
                   settings=settings)
        counts.deadline += 1
    session.flush()
    return counts
