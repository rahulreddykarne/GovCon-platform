"""Reminder/escalation state belongs to the current review cycle."""
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from govcon.collaboration.escalation import run_review_escalations
from govcon.collaboration.review_sessions import ensure_review_session
from govcon.config import Settings
from govcon.models import Notification, ReviewAssignment
from govcon.workflow.invalidation import reopen_review
from test_sourcing_company import db, opportunity, user


def review_fixture(db, status="assigned"):
    opp = opportunity(db)
    owner, _ = user(db, "owner")
    reviewer, _ = user(db)
    review = ensure_review_session(db, opportunity_id=opp.id)
    review.status = "under_review"
    now = datetime.now(UTC)
    assignment = ReviewAssignment(opportunity_id=opp.id, user_id=reviewer.id, status=status,
                                  assigned_at=now - timedelta(days=4),
                                  completed_at=now - timedelta(hours=1) if status == "complete" else None)
    db.add(assignment)
    db.commit()
    settings = Settings(_env_file=None, review_reminder_hours=24, review_overdue_hours=48,
                        email_notifications_enabled=False)
    return opp, owner, reviewer, assignment, now, settings


def notices(db, opp, recipient, kind):
    return db.scalar(select(func.count()).select_from(Notification).where(
        Notification.opportunity_id == opp.id, Notification.user_id == recipient.id,
        Notification.notification_type == kind))


def test_current_initial_cycle_reminds_and_escalates_once(db):
    opp, owner, reviewer, assignment, now, settings = review_fixture(db)
    run_review_escalations(db, settings=settings, now=now)
    assert notices(db, opp, reviewer, "review_reminder") == 1
    assert notices(db, opp, owner, "review_overdue_escalation") == 1
    row = db.scalar(select(Notification).where(Notification.opportunity_id == opp.id,
                    Notification.user_id == owner.id, Notification.notification_type == "review_overdue_escalation"))
    assert row.payload["assigned_hours_ago"] == 96 and row.payload["reviewer_user_id"] == reviewer.id
    run_review_escalations(db, settings=settings, now=now + timedelta(minutes=1))
    assert notices(db, opp, reviewer, "review_reminder") == 1
    assert notices(db, opp, owner, "review_overdue_escalation") == 1
    db.rollback()


def test_current_uncompleted_assignment_is_not_reopened_or_reset(db):
    opp, owner, reviewer, assignment, now, settings = review_fixture(db)
    assignment.last_reminded_at = assignment.escalated_at = now
    db.flush()
    assert reopen_review(db, opp.id, reason="material source change") is False
    assert assignment.status == "assigned" and assignment.reopened_at is None
    assert assignment.last_reminded_at == assignment.escalated_at == now
    db.rollback()


def test_reopened_cycle_does_not_immediately_use_original_assignment_age(db):
    opp, owner, reviewer, assignment, now, settings = review_fixture(db, "complete")
    original_assigned = assignment.assigned_at
    assert reopen_review(db, opp.id, reason="material amendment") is True
    assert assignment.assigned_at == original_assigned
    run_review_escalations(db, settings=settings, now=assignment.reopened_at)
    assert notices(db, opp, reviewer, "review_reminder") == 0, "a newly reopened review was already overdue"
    assert notices(db, opp, owner, "review_overdue_escalation") == 0
    db.rollback()


def test_reopened_previously_escalated_cycle_escalates_once_again(db):
    opp, owner, reviewer, assignment, now, settings = review_fixture(db, "complete")
    assignment.last_reminded_at = now - timedelta(hours=1)
    assignment.escalated_at = now - timedelta(days=3)
    db.flush()
    assert reopen_review(db, opp.id, reason="material amendment") is True
    reopened = assignment.reopened_at
    due = reopened + timedelta(hours=49)
    run_review_escalations(db, settings=settings, now=due)
    assert notices(db, opp, owner, "review_overdue_escalation") == 1, "the previous cycle suppressed the new escalation"
    assert notices(db, opp, reviewer, "review_reminder") == 1
    assert assignment.escalated_at == assignment.last_reminded_at == due
    run_review_escalations(db, settings=settings, now=due + timedelta(minutes=1))
    assert notices(db, opp, owner, "review_overdue_escalation") == 1
    assert notices(db, opp, reviewer, "review_reminder") == 1
    db.rollback()
