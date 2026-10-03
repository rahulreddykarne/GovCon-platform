"""Preparation service writes must obey cancellation and checkpoint fencing."""
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from test_preparation import new_opportunity

from govcon.audit import record_audit
from govcon.db import session_scope
from govcon.models import (
    AIAnalysis,
    AuditEvent,
    ComplianceRun,
    Opportunity,
    ReviewSession,
    Task,
)
from govcon.tasks.worker import run_once
from govcon.workflow.preparation import PREPARATION_TASK, STEPS, queue_preparation


def queued(db, opp, only=None):
    task, _ = queue_preparation(db, opportunity_id=opp.id, actor_user_id=None)
    if only:
        task.checkpoint = {"completed_steps": [s for s in STEPS if s != only]}
    db.commit()
    return task.id


def test_current_service_results_and_checkpoint_shapes_are_preserved(db):
    opp = new_opportunity(db)
    task_id = queued(db, opp)
    assert run_once(task_id=task_id) == (task_id, "succeeded")
    db.expire_all()
    task = db.get(Task, task_id)
    assert task.checkpoint["completed_steps"] == list(STEPS)
    data = task.checkpoint["data"]
    assert data["summary"]["analysis_id"] is None and "note" in data["summary"]
    assert data["compliance"]["status"] == "incomplete"
    assert db.get(ComplianceRun, data["compliance"]["matrix_run_id"]).opportunity_id == opp.id
    assert db.get(AIAnalysis, data["decision"]["analysis_id"]).opportunity_id == opp.id
    assert db.scalar(select(ReviewSession).where(ReviewSession.opportunity_id == opp.id)) is not None


@pytest.mark.parametrize("step", ["summary", "compliance", "decision"])
@pytest.mark.parametrize("loss", ["cancel", "lease", "source", "expired"])
def test_service_writes_are_discarded_when_work_is_invalidated(db, client, monkeypatch, step, loss):
    from importlib import import_module

    from test_web_ui import _make_user
    opp = new_opportunity(db)
    _, token = _make_user(db, f"fence-{uuid4().hex}@example.test", "owner")
    client.cookies.set("govcon_session", token)
    task_id = queued(db, opp, only=step)
    module_name, function_name = {
        "summary": ("govcon.enrich.summarize", "run_solicitation_analysis"),
        "compliance": ("govcon.compliance.pipeline", "run_compliance_pipeline"),
        "decision": ("govcon.decision.engine", "run_preliminary_decision_package"),
    }[step]
    module = import_module(module_name)
    original = getattr(module, function_name)

    def concurrent_change():
        if loss == "cancel":
            assert client.post(f"/ops/tasks/{task_id}/cancel").status_code == 303
        elif loss == "lease":
            with session_scope() as external:
                task = external.get(Task, task_id)
                task.claim_token += 1
                task.lease_owner = "replacement-worker"
        elif loss == "source":
            with session_scope() as external:
                external.get(Opportunity, opp.id).raw_hash = uuid4().hex
        elif loss == "expired":
            with session_scope() as external:
                external.get(Task, task_id).lease_expires_at = datetime.now(UTC) - timedelta(microseconds=1)

    def invalidate(session, *args, **kwargs):
        from govcon.ai.replay import active_recorder
        record_audit(session, action_type="fv03_service_write", opportunity_id=opp.id)
        # The change lands while an AI call runs between recorded passes: no
        # transaction is open then. Inside a pass, the opportunity and task
        # locks make a concurrent change wait for the pass instead.
        active_recorder().call(("fv03", step, loss), concurrent_change)
        return original(session, *args, **kwargs)

    monkeypatch.setattr(module, function_name, invalidate)
    run_once(task_id=task_id)
    db.expire_all()
    count = db.scalar(select(func.count()).select_from(AuditEvent).where(
        AuditEvent.opportunity_id == opp.id, AuditEvent.action_type == "fv03_service_write"))
    assert count == 0, "invalidated service transaction committed business writes"
    assert step not in db.get(Task, task_id).checkpoint["completed_steps"]
    if step == "compliance":
        assert db.scalar(select(func.count()).select_from(ComplianceRun).where(
            ComplianceRun.opportunity_id == opp.id)) == 0


def test_crash_after_service_commit_does_not_repeat_the_service(db, monkeypatch):
    from govcon.tasks.registry import get_handler
    opp = new_opportunity(db)
    task_id = queued(db, opp, only="compliance")
    step = next(s for s in get_handler(PREPARATION_TASK).steps if s.name == "compliance")
    original = step.execute
    calls = []

    def crash_after_commit(inputs, ctx):
        output = original(inputs, ctx)
        calls.append(output)
        if len(calls) == 1:
            raise RuntimeError("crash between service commit and outer worker publication")
        return output

    monkeypatch.setattr(step, "execute", crash_after_commit)
    assert run_once(task_id=task_id) == (task_id, "retrying")
    with session_scope() as external:
        external.get(Task, task_id).next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    assert run_once(task_id=task_id) == (task_id, "succeeded")
    assert len(calls) == 1, "already committed compliance work ran again after retry"
