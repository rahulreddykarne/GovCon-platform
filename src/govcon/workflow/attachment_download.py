"""Download-only retry after a SAM 429. Never starts analysis, compliance, decision, or market."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.enrich.safe_fetch import MAX_RETRY_AFTER_SECONDS
from govcon.models import AIAnalysis, Opportunity, OpportunityEvent, StoredFile, Task
from govcon.tasks import queue
from govcon.tasks.registry import (
    Step,
    StepContext,
    TaskHandler,
    register,
    required_opportunity_id,
)
from govcon.workflow.preparation import (
    _documents_execute,
    _documents_prepare,
    _documents_publish,
    _opportunity,
)

DOWNLOAD_TASK = "attachment_download"
DOCUMENT_READY_EVENT = "document_ready"
DOCUMENT_READY_MESSAGE = "document now available, rerun analysis?"
BATCH_REVISION = {"reason": "attachment_rate_limited", "scope": "batch"}


def queue_attachment_download(
    session: Session,
    *,
    opportunity_id: int | None = None,
    retry_after: float | None = None,
    actor_user_id: int | None = None,
    opportunity_ids: list[int] | None = None,
) -> tuple[Task, bool]:
    """Queue one documents-only fetch for the rate-limited batch. Does not start AI."""
    ids: list[int] = []
    for value in list(opportunity_ids or []) + ([opportunity_id] if opportunity_id is not None else []):
        if value not in ids:
            ids.append(int(value))
    task, created = queue.enqueue(
        session,
        task_type=DOWNLOAD_TASK,
        opportunity_id=None,
        input_revision=BATCH_REVISION,
        payload={"retry_reason": "rate_limited", "opportunity_ids": ids},
        actor_user_id=actor_user_id,
    )
    delay = 60.0 if retry_after is None else float(retry_after)
    delay = min(MAX_RETRY_AFTER_SECONDS, max(1.0, delay))
    proposed = datetime.now(UTC) + timedelta(seconds=delay)
    if task.next_attempt_at is None or created:
        task.next_attempt_at = proposed
    elif proposed > task.next_attempt_at:
        task.next_attempt_at = proposed
    payload = dict(task.payload or {})
    merged = list(payload.get("opportunity_ids") or [])
    for oid in ids:
        if oid not in merged:
            merged.append(oid)
    payload["opportunity_ids"] = merged
    payload["retry_reason"] = "rate_limited"
    task.payload = payload
    return task, created


def _download_ids(task: Task) -> list[int]:
    payload = task.payload if isinstance(task.payload, dict) else {}
    raw = payload.get("opportunity_ids") or []
    ids: list[int] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, int):
                ids.append(item)
            elif isinstance(item, str) and item.isdigit():
                ids.append(int(item))
    if task.opportunity_id is not None and task.opportunity_id not in ids:
        ids.append(task.opportunity_id)
    return ids


def _is_batch(task: Task) -> bool:
    revision = task.input_revision if isinstance(task.input_revision, dict) else {}
    return revision.get("scope") == "batch" or task.opportunity_id is None


@dataclass
class _BatchDownload:
    opportunity_ids: list[int] = field(default_factory=list)
    completed_ids: list[int] = field(default_factory=list)


def _prepare(session: Session, task: Task, ctx: StepContext) -> Any:
    if _is_batch(task):
        ids = _download_ids(task)
        if not ids:
            from govcon.tasks.errors import TaskCancelled
            raise TaskCancelled("the batch download has no remaining opportunities")
        return _BatchDownload(opportunity_ids=ids)
    return _documents_prepare(session, task, ctx)


def _execute(docs: Any, ctx: StepContext) -> Any:
    if isinstance(docs, _BatchDownload):
        from govcon.db import session_scope
        from govcon.enrich.attachments import download_attachments
        from govcon.ingest.sam_quota import begin_sam_budget

        begin_sam_budget(ctx.settings.sam_request_budget_per_run)
        completed: list[int] = []
        for oid in docs.opportunity_ids:
            with session_scope(ctx.settings) as session:
                opportunity = session.get(Opportunity, oid)
                if opportunity is None:
                    continue
                download_attachments(
                    session, opportunity, settings=ctx.settings, schedule_retry=False,
                )
            completed.append(oid)
        docs.completed_ids = completed
        return docs
    return _documents_execute(docs, ctx)


def _emit_ready(session: Session, opportunity_id: int) -> None:
    ready = [
        {"id": row.id, "filename": row.filename, "url": row.url}
        for row in session.scalars(
            select(StoredFile).where(
                StoredFile.opportunity_id == opportunity_id,
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
            opportunity_id=opportunity_id,
            event_type=DOCUMENT_READY_EVENT,
            field_name="attachments",
            new_value={"message": DOCUMENT_READY_MESSAGE, "files": ready},
        )
    )


def _publish(session: Session, task: Task, docs: Any, ctx: StepContext) -> None:
    if isinstance(docs, _BatchDownload):
        for oid in docs.completed_ids:
            _emit_ready(session, oid)
        ctx.result = {"opportunities": docs.completed_ids, "retried": len(docs.completed_ids)}
        return
    _documents_publish(session, task, docs, ctx)
    opportunity = _opportunity(session, task)
    _emit_ready(session, opportunity.id)


register(TaskHandler(task_type=DOWNLOAD_TASK, steps=[
    Step("documents", prepare=_prepare, execute=_execute, publish=_publish,
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
