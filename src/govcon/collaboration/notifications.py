"""In-app notification records. Email delivery is configured later."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from govcon.config import Settings, get_settings
from govcon.models import Notification

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
    }
)


def notify(
    session: Session,
    *,
    user_id: int,
    notification_type: str,
    opportunity_id: int | None = None,
    payload: dict | None = None,
    settings: Settings | None = None,
) -> Notification:
    """Store an in-app notification. Does not send email in Phase 0."""
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
    if not settings.email_configured:
        logger.info(
            "notification_stored type=%s user_id=%s email_delivery=disabled",
            notification_type,
            user_id,
        )
    return row
