"""Reviewer comment side opinions use the existing durable AI-analysis worker."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy.orm import Session

from govcon.ai.analysis_types import AnalysisType
from govcon.ai.schemas import ReviewerCommentValidationV1
from govcon.ai.structured import (
    PreparedCall,
    checked_output,
    persist_structured_result,
    prepare_structured_call,
)
from govcon.audit import record_audit
from govcon.collaboration.ai_comment_review import (
    apply_ai_validation_to_comment,
    comment_validation_variables,
)
from govcon.collaboration.notifications import notify
from govcon.intelligence.analysis_tasks import ANALYSIS_TASK
from govcon.models import Pursuit, ReviewComment, Task
from govcon.security.classification import DataClassification
from govcon.tasks.errors import TaskCancelled, TaskSuperseded
from govcon.tasks.queue import enqueue
from govcon.tasks.registry import StepContext
from govcon.workflow.source_revision import current_source_revision

COMMENT_ANALYSIS_KIND = "reviewer_comment_validation"


def comment_inputs(session: Session, comment: ReviewComment) -> dict[str, Any]:
    variables = comment_validation_variables(session, comment=comment)
    encoded = json.dumps(variables, sort_keys=True, default=str).encode()
    return {"kind": COMMENT_ANALYSIS_KIND, "comment_id": comment.id,
            "source_revision": str(current_source_revision(session, comment.opportunity_id)),
            "context_hash": hashlib.sha256(encoded).hexdigest()}


def queue_comment_validation(session: Session, *, comment: ReviewComment) -> tuple[Task, bool]:
    return enqueue(session, task_type=ANALYSIS_TASK, opportunity_id=comment.opportunity_id,
                   input_revision=comment_inputs(session, comment),
                   payload={"kind": COMMENT_ANALYSIS_KIND, "comment_id": comment.id},
                   actor_user_id=comment.user_id)


def _current_comment(session: Session, task: Task, ctx: StepContext) -> ReviewComment:
    from sqlalchemy import select

    comment = session.get(ReviewComment, ctx.payload["comment_id"])
    if comment is None or comment.opportunity_id != task.opportunity_id:
        raise TaskCancelled("The review comment no longer exists.")
    pursuit = session.scalar(select(Pursuit).where(Pursuit.opportunity_id == task.opportunity_id))
    if pursuit and pursuit.stage == "cancelled":
        raise TaskCancelled("The pursuit was cancelled.")
    inputs = comment_inputs(session, comment)
    if inputs != ctx.input_revision:
        raise TaskSuperseded("The comment evidence changed; validation was requeued for current evidence.",
                             input_revision=inputs)
    return comment


def prepare_validation(session: Session, task: Task, ctx: StepContext) -> PreparedCall:
    comment = _current_comment(session, task, ctx)
    return prepare_structured_call(
        session, classification=DataClassification.PROPRIETARY, settings=ctx.settings,
        opportunity_id=comment.opportunity_id, prompt_name="reviewer_comment_validation",
        analysis_type=AnalysisType.REVIEWER_COMMENT_VALIDATION,
        variables=comment_validation_variables(session, comment=comment),
        context_manifest={"opportunity_id": comment.opportunity_id, "comment_id": comment.id},
    )


def publish_validation(session: Session, task: Task, prepared: PreparedCall, executed: Any, ctx: StepContext) -> None:
    comment = _current_comment(session, task, ctx)
    validation = checked_output(executed.output, ReviewerCommentValidationV1)
    analysis = persist_structured_result(session, prepared, executed).analysis
    apply_ai_validation_to_comment(comment, validation)
    if validation.position == "insufficient_evidence":
        notify(session, user_id=comment.user_id, opportunity_id=comment.opportunity_id,
               notification_type="ai_flagged_comment_needs_evidence", payload={"comment_id": comment.id})
    record_audit(session, action_type="review_comment_validated", user_id=comment.user_id,
                 opportunity_id=comment.opportunity_id, entity_type="review_comments", entity_id=comment.id,
                 new_value={"task_id": task.id, "analysis_id": analysis.id, "ai_position": validation.position})
    ctx.result = {"comment_id": comment.id, "analysis_id": analysis.id}
