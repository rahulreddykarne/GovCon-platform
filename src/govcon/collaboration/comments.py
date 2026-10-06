"""Threaded review comments and AI validation orchestration."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.audit import record_audit
from govcon.collaboration.ai_comment_review import (
    AICommentValidationError,
    apply_ai_validation_to_comment,
    validate_comment_with_ai,
)
from govcon.collaboration.assignments import assignment_for_user, start_assignment
from govcon.collaboration.notifications import notify
from govcon.models import ReviewAssignment, ReviewComment

_NON_SUBSTANTIVE = {
    "ok",
    "looks good",
    "lgtm",
    "agree",
    "disagree",
    "yes",
    "no",
}


def is_substantive_comment(body: str) -> bool:
    text = (body or "").strip().lower()
    if not text:
        return False
    if text in _NON_SUBSTANTIVE:
        return False
    if len(text) < 16:
        return False
    return len(text.split()) >= 3


def list_comments(session: Session, opportunity_id: int) -> list[ReviewComment]:
    return list(
        session.scalars(
            select(ReviewComment)
            .where(ReviewComment.opportunity_id == opportunity_id)
            .order_by(ReviewComment.id)
        ).all()
    )


def add_comment(
    session: Session,
    *,
    opportunity_id: int,
    user_id: int,
    body: str,
    topic: str | None = None,
    parent_comment_id: int | None = None,
    source_refs: dict | None = None,
    user_recommendation: str | None = None,
    validate_with_ai: bool = True,
    defer_ai_validation: bool = False,
) -> ReviewComment:
    if not body or not body.strip():
        raise ValueError("comment body is required")
    user_recommendation = "needs_more_info" if user_recommendation == "needs_info" else user_recommendation
    if user_recommendation not in {None, "bid", "no_bid", "needs_more_info"}:
        raise ValueError("unknown comment recommendation")
    assignment = assignment_for_user(
        session, opportunity_id=opportunity_id, user_id=user_id
    )
    if assignment is None:
        raise ValueError("user is not assigned to review this opportunity")
    start_assignment(session, opportunity_id=opportunity_id, user_id=user_id)

    row = ReviewComment(
        opportunity_id=opportunity_id,
        user_id=user_id,
        parent_comment_id=parent_comment_id,
        topic=topic,
        body=body.strip(),
        source_refs=source_refs,
        user_recommendation=user_recommendation,
    )
    session.add(row)
    session.flush()

    if validate_with_ai and is_substantive_comment(row.body):
        if defer_ai_validation:
            from govcon.collaboration.comment_tasks import queue_comment_validation

            queue_comment_validation(session, comment=row)
        else:
            _validate_inline(session, row, user_id=user_id, opportunity_id=opportunity_id)

    _notify_other_reviewers(session, assignment, row)
    record_audit(
        session,
        action_type="review_comment_added",
        user_id=user_id,
        opportunity_id=opportunity_id,
        entity_type="review_comments",
        entity_id=row.id,
        new_value={
            "topic": row.topic,
            "body": row.body,
            "parent_comment_id": row.parent_comment_id,
            "ai_position": row.ai_position,
        },
    )
    session.flush()
    return row


def _validate_inline(session: Session, row: ReviewComment, *, user_id: int, opportunity_id: int) -> None:
    try:
        validation = validate_comment_with_ai(session, comment=row)
        apply_ai_validation_to_comment(row, validation)
    except AICommentValidationError as exc:
        row.ai_position = None
        row.ai_confidence = None
        row.ai_reason = f"AI validation failed ({exc.reason}): {exc.detail}"
        row.ai_supporting_evidence = None
        row.ai_contradicting_evidence = None
        row.ai_missing_information = None
        row.ai_suggested_action = None
    else:
        if row.ai_position == "insufficient_evidence":
            notify(
                session,
                user_id=user_id,
                opportunity_id=opportunity_id,
                notification_type="ai_flagged_comment_needs_evidence",
                payload={"comment_id": row.id},
            )


def revise_comment(
    session: Session,
    *,
    comment_id: int,
    user_id: int,
    new_body: str,
    reason: str | None = None,
    validate_with_ai: bool = True,
) -> ReviewComment:
    """Preserve history by appending a revision comment instead of overwriting."""
    prior = session.get(ReviewComment, comment_id)
    if prior is None:
        raise ValueError("comment not found")
    if prior.user_id != user_id:
        raise ValueError("only the comment author may revise their comment")
    revised = add_comment(
        session,
        opportunity_id=prior.opportunity_id,
        user_id=user_id,
        body=new_body,
        topic=prior.topic,
        parent_comment_id=prior.id,
        source_refs=prior.source_refs,
        user_recommendation=prior.user_recommendation,
        validate_with_ai=validate_with_ai,
    )
    record_audit(
        session,
        action_type="review_comment_revised",
        user_id=user_id,
        opportunity_id=prior.opportunity_id,
        entity_type="review_comments",
        entity_id=revised.id,
        old_value={"comment_id": prior.id, "body": prior.body},
        new_value={"comment_id": revised.id, "body": revised.body, "reason": reason},
    )
    return revised


def reviewer_comment_count(
    session: Session, *, opportunity_id: int, user_id: int
) -> int:
    return len(
        session.scalars(
            select(ReviewComment.id).where(
                ReviewComment.opportunity_id == opportunity_id,
                ReviewComment.user_id == user_id,
            )
        ).all()
    )


def _notify_other_reviewers(
    session: Session, assignment: ReviewAssignment, comment: ReviewComment
) -> None:
    reviewers = session.scalars(
        select(ReviewAssignment).where(
            ReviewAssignment.opportunity_id == comment.opportunity_id
        )
    ).all()
    for row in reviewers:
        if row.user_id == comment.user_id:
            continue
        notify(
            session,
            user_id=row.user_id,
            opportunity_id=comment.opportunity_id,
            notification_type="new_reviewer_comment",
            payload={
                "comment_id": comment.id,
                "from_user_id": comment.user_id,
                "at": datetime.now(UTC).isoformat(),
            },
        )
