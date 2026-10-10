"""Download-only retry after a SAM 429. Never starts analysis, compliance, decision, or market."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.models import AIAnalysis, OpportunityEvent, StoredFile, Task
from govcon.tasks import queue
from govcon.tasks.registry import Step, StepContext, TaskHandler, register, required_opportunity_id
from govcon.workflow.preparation import (
    _documents_execute,
    _documents_prepare,
    _documents_publish,
    _opportunity,
)

DOWNLOAD_TASK = "attachment_download"
DOCUMENT_READY_EVENT = "document_ready"
DOCUMENT_READY_MESSAGE = "document now available, rerun analysis?"


def queue_attachment_download(
    session: Session,
    *,
    opportunity_id: int,
    retry_after: float | None = None,
    actor_user_id: int | None = None,
) -> tuple[Task, bool]:
    """Queue a documents-only fetch. Does not start preparation or AI."""
    from datetime import UTC, datetime, timedelta

    task, created = queue.enqueue(
        session,
        task_type=DOWNLOAD_TASK,
        opportunity_id=opportunity_id,
        input_revision={"reason": "attachment_rate_limited"},
        payload={"retry_reason": "rate_limited"},
        actor_user_id=actor_user_id,
    )
    delay = max(1.0, retry_after if retry_after is not None else 60.0)
    task.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
    return task, created


def _publish(session: Session, task: Task, docs: Any, ctx: StepContext) -> None:
    _documents_publish(session, task, docs, ctx)
    opportunity = _opportunity(session, task)
    ready = [
        {"id": row.id, "filename": row.filename, "url": row.url}
        for row in session.scalars(
            select(StoredFile).where(
                StoredFile.opportunity_id == opportunity.id,
                StoredFile.active.is_(True),
                StoredFile.extracted_text.is_not(None),
            )
        )
        if (row.extracted_text or "").strip()
    ]
    if not ready:
        return
    session.add(
        OpportunityEvent(
            opportunity_id=opportunity.id,
            event_type=DOCUMENT_READY_EVENT,
            field_name="attachments",
            new_value={"message": DOCUMENT_READY_MESSAGE, "files": ready},
        )
    )


register(TaskHandler(task_type=DOWNLOAD_TASK, steps=[
    Step("documents", prepare=_documents_prepare, execute=_documents_execute, publish=_publish,
         timeout_seconds=3600),
]))


def document_ready_notice(session: Session, opportunity_id: int) -> dict[str, Any] | None:
    """Banner for a human: a download finished after the last analysis, so they may rerun."""
    event = session.scalar(
        select(OpportunityEvent)
        .where(
            OpportunityEvent.opportunity_id == opportunity_id,
            OpportunityEvent.event_type == DOCUMENT_READY_EVENT,
        )
        .order_by(OpportunityEvent.detected_at.desc())
        .limit(1)
    )
    if event is None:
        return None
    last_analysis = session.scalar(
        select(AIAnalysis)
        .where(AIAnalysis.opportunity_id == opportunity_id)
        .order_by(AIAnalysis.created_at.desc())
        .limit(1)
    )
    if last_analysis is not None and last_analysis.created_at is not None and event.detected_at <= last_analysis.created_at:
        return None
    payload = event.new_value if isinstance(event.new_value, dict) else {}
    return {
        "message": payload.get("message") or DOCUMENT_READY_MESSAGE,
        "detected_at": event.detected_at,
        "files": payload.get("files") or [],
        "event_id": event.id,
    }


def require_download_opportunity(task: Task) -> int:
    return required_opportunity_id(task.opportunity_id)
