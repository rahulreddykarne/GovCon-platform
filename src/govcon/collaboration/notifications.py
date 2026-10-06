"""In-app notification records. Email delivery is configured later."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from govcon.config import Settings, get_settings
from govcon.models import Notification, User

logger = logging.getLogger("govcon.collaboration.notifications")

NOTIFICATION_TYPES = frozenset(
    {
        "review_assigned",
        "reviewer_completed",
        "new_reviewer_comment",
        "ai_flagged_comment_needs_evidence",
        "review_quorum_satisfied",
        "second_review_required",
        "second_review_requested",
        "review_reassigned",
        "approval_pending",
        "material_amendment_after_review",
        "proposal_package_generated",
        "submission_ready",
        "review_reminder",
        "review_overdue_escalation",
        "deadline_escalation",
        "auto_pursued",
        "registration_expiring",
        "outcome_suggested",
    }
)
# Types that ask a person to act; they can be acknowledged explicitly (ADR-070).
ACTION_REQUIRED_TYPES = frozenset({
    "review_assigned", "second_review_required", "approval_pending", "review_reminder",
    "review_overdue_escalation", "deadline_escalation", "registration_expiring", "outcome_suggested",
})


def notify(
    session: Session,
    *,
    user_id: int,
    notification_type: str,
    opportunity_id: int | None = None,
    payload: dict | None = None,
    settings: Settings | None = None,
) -> Notification:
    """Store an in-app notification and, when enabled, queue its email (ADR-070).

    With ``NOTIFY_EMAIL_ENABLED`` and ``SMTP_HOST`` set, a delivery row and a
    ``notification_email`` task are added in the caller's transaction, so the
    notification and its delivery commit together.
    """
    if notification_type not in NOTIFICATION_TYPES:
        raise ValueError(f"unknown notification type: {notification_type}")
    row = Notification(
        user_id=user_id,
        opportunity_id=opportunity_id,
        notification_type=notification_type,
        payload=payload,
    )
    session.add(row)
    session.flush()
    settings = settings or get_settings()
    if settings.notify_email_enabled and settings.smtp_host:
        _queue_email(session, row, settings)
    elif not settings.email_configured:
        logger.info(
            "notification_stored type=%s user_id=%s email_delivery=disabled",
            notification_type,
            user_id,
        )
    return row


def _queue_email(session: Session, row: Notification, settings: Settings) -> None:
    from govcon.models import NotificationDelivery
    from govcon.tasks import queue

    user = session.get(User, row.user_id)
    if user is None or not user.is_active or not user.email:
        return
    delivery = NotificationDelivery(notification_id=row.id, recipient=user.email)
    session.add(delivery)
    session.flush()
    queue.enqueue(
        session, task_type="notification_email", opportunity_id=row.opportunity_id,
        input_revision={"delivery_id": delivery.id}, payload={"delivery_id": delivery.id}, settings=settings,
    )


def acknowledge(session: Session, *, notification_id: int, user: "User") -> Notification:
    """Record that the user has seen and accepted an action-required notification."""
    from datetime import UTC, datetime

    row = session.get(Notification, notification_id, with_for_update=True)
    if row is None or row.user_id != user.id:
        raise ValueError("notification not found")
    now = datetime.now(UTC)
    row.read_at = row.read_at or now
    row.acknowledged_at = row.acknowledged_at or now
    session.flush()
    return row
