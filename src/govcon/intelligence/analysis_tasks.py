"""Queue market, supplier and pricing analyses as durable tasks (ADR-064).

The web request (or CLI) checks inputs and policy at once, so a missing
supplier or a blocked data class is reported immediately, then queues the
work; a worker makes the AI call with no transaction open.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.config import Settings, get_settings
from govcon.intelligence.ai_analyses import ANALYSIS_KINDS, prepare_analysis
from govcon.models import Pursuit, Task
from govcon.tasks import queue
from govcon.workflow.proposal_generation import commercial_hash
from govcon.workflow.source_revision import current_source_revision

ANALYSIS_TASK = "ai_analysis"


def analysis_inputs(session: Session, opportunity_id: int, kind: str) -> dict[str, Any]:
    """What an analysis result depends on; a change makes a queued run stale."""
    inputs: dict[str, Any] = {
        "kind": kind,
        "source_revision": str(current_source_revision(session, opportunity_id)),
    }
    if kind in ("supplier", "pricing"):
        from govcon.sourcing.records import sourcing_revision

        pursuit = session.scalar(select(Pursuit).where(Pursuit.opportunity_id == opportunity_id))
        inputs["commercial_hash"] = commercial_hash(pursuit)
        inputs["quotes"] = sourcing_revision(session, opportunity_id)
    return inputs


def queue_analysis(
    session: Session,
    *,
    opportunity_id: int,
    kind: str,
    actor_user_id: int | None,
    settings: Settings | None = None,
) -> tuple[Task, bool]:
    """Check the analysis can run, then queue it in the caller's transaction.

    Raises ``AnalysisInputMissing`` or ``StructuredCallError`` (policy) before
    queueing anything. The same analysis on the same inputs is queued once
    while active.
    """
    if kind not in ANALYSIS_KINDS:
        raise ValueError(f"unknown analysis kind: {kind!r}")
    settings = settings or get_settings()
    prepare_analysis(session, opportunity_id, kind, settings=settings)
    return queue.enqueue(
        session,
        task_type=ANALYSIS_TASK,
        opportunity_id=opportunity_id,
        input_revision=analysis_inputs(session, opportunity_id, kind),
        payload={"kind": kind},
        actor_user_id=actor_user_id,
        settings=settings,
    )


def latest_analysis_tasks(session: Session, opportunity_id: int) -> dict[str, Task]:
    """The most recent task of each analysis kind for an opportunity."""
    latest: dict[str, Task] = {}
    rows = session.scalars(
        select(Task)
        .where(Task.task_type == ANALYSIS_TASK, Task.opportunity_id == opportunity_id)
        .order_by(Task.id.desc())
        .limit(50)
    )
    for task in rows:
        latest.setdefault((task.payload or {}).get("kind"), task)
    return latest
