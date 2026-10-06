"""``ai_analysis``: market, supplier or pricing analysis as a durable task (ADR-064)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from govcon.ai.structured import (
    PreparedCall,
    execute_prepared_call,
    persist_structured_result,
)
from govcon.audit import record_audit
from govcon.collaboration.comment_tasks import (
    COMMENT_ANALYSIS_KIND,
    prepare_validation,
    publish_validation,
)
from govcon.db import shared_session_factory
from govcon.intelligence.ai_analyses import AnalysisInputMissing, prepare_analysis
from govcon.intelligence.analysis_tasks import ANALYSIS_TASK, analysis_inputs
from govcon.models import Task
from govcon.tasks.errors import TaskBlocked, TaskSuperseded
from govcon.tasks.registry import (
    Step,
    StepContext,
    TaskHandler,
    register,
    required_opportunity_id,
)

_INPUT_ACTION = {
    "supplier": ("Enter a supplier or sourcing cost under Pursuit commercial facts on the Products and "
                 "Suppliers tab, then retry the supplier analysis."),
    "pricing": ("Enter a quote price or sourcing cost under Pursuit commercial facts on the Pricing tab, "
                "then retry the pricing analysis."),
}


@dataclass
class _Call:
    prepared: PreparedCall
    executed: Any = None


def _check_inputs(session: Session, task: Task, ctx: StepContext) -> str:
    kind = ctx.payload["kind"]
    current = analysis_inputs(session, required_opportunity_id(task.opportunity_id), kind)
    if current != ctx.input_revision:
        raise TaskSuperseded(
            "the source documents or pursuit facts changed after this analysis was queued; "
            "re-queued for the current inputs",
            input_revision=current,
        )
    return kind


def _prepare(session: Session, task: Task, ctx: StepContext) -> _Call:
    if ctx.payload.get("kind") == COMMENT_ANALYSIS_KIND:
        return _Call(prepared=prepare_validation(session, task, ctx))
    kind = _check_inputs(session, task, ctx)
    try:
        return _Call(prepared=prepare_analysis(session, required_opportunity_id(task.opportunity_id), kind, settings=ctx.settings))
    except AnalysisInputMissing as exc:
        raise TaskBlocked(str(exc), owner_role="approver",
                          next_action=_INPUT_ACTION.get(kind, "Record the missing inputs, then retry.")) from exc


def _execute(call: _Call, ctx: StepContext) -> _Call:
    engine = shared_session_factory(ctx.settings).kw["bind"]
    call.executed = execute_prepared_call(call.prepared, settings=ctx.settings, engine=engine)
    return call


def _publish(session: Session, task: Task, call: _Call, ctx: StepContext) -> None:
    if ctx.payload.get("kind") == COMMENT_ANALYSIS_KIND:
        publish_validation(session, task, call.prepared, call.executed, ctx)
        return
    kind = _check_inputs(session, task, ctx)
    analysis = persist_structured_result(session, call.prepared, call.executed).analysis
    record_audit(
        session, action_type="ai_analysis_completed", user_id=task.created_by_user_id,
        opportunity_id=required_opportunity_id(task.opportunity_id), entity_type="ai_analyses", entity_id=analysis.id,
        new_value={"kind": kind, "task_id": task.id},
    )
    ctx.result = {"kind": kind, "analysis_id": analysis.id}


register(TaskHandler(
    task_type=ANALYSIS_TASK,
    steps=[Step(name="analyze", prepare=_prepare, execute=_execute, publish=_publish, timeout_seconds=600)],
))
