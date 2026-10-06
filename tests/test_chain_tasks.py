"""Roadmap gap 2: scheduler chains run as durable tasks and resume after a crash (ADR-063)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text, update

from govcon.db import make_engine, session_scope
from govcon.models import SchedulerJobRun, Task
from govcon.scheduler import chains
from govcon.scheduler.chain_tasks import CHAIN_TASK, queue_chain
from govcon.scheduler.jobs import StepResult
from govcon.tasks.worker import run_once

pytestmark = pytest.mark.usefixtures("upgraded_engine")


class _Crash(BaseException):
    """Stands in for the worker process dying: nothing in the worker catches it."""


@pytest.fixture()
def test_chain(monkeypatch):
    """A three-step chain whose steps count their calls; ``crash_on`` kills the worker."""
    calls = {"a": 0, "b": 0, "c": 0}
    state = {"crash_on": None, "fail": set()}

    def make(name):
        def step(session):
            calls[name] += 1
            if state["crash_on"] == name:
                raise _Crash(name)
            status = "failed" if name in state["fail"] else "succeeded"
            return StepResult(step=name, status=status, inserted=1, error="boom" if status == "failed" else None)
        return step

    name = f"resume_test_{datetime.now(UTC).timestamp():.0f}"
    monkeypatch.setitem(chains.CHAIN_DEFINITIONS, name, chains.ChainDef(
        name=name, description="test", cron="manual", steps=["a", "b", "c"]))
    monkeypatch.setattr(chains, "_STEP_FUNCTIONS", {**chains._STEP_FUNCTIONS,
                                                    "a": make("a"), "b": make("b"), "c": make("c")})
    return name, calls, state


def _queue(chain_name, slot="manual"):
    with session_scope() as db:
        task, _ = queue_chain(db, chain_name, trigger="test", slot=slot)
        return task.id


def _task(task_id):
    with session_scope() as db:
        return db.get(Task, task_id)


def _expire_lease(task_id):
    with session_scope() as db:
        db.execute(update(Task).where(Task.id == task_id).values(
            lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)))


def test_crashed_chain_resumes_at_the_next_unfinished_step(test_chain):
    chain_name, calls, state = test_chain
    task_id = _queue(chain_name)
    state["crash_on"] = "b"
    with pytest.raises(_Crash):
        run_once(task_id=task_id)
    crashed = _task(task_id)
    assert crashed.status == "running" and crashed.checkpoint["completed_steps"] == ["a"]
    run_id = crashed.scheduler_job_run_id
    with session_scope() as db:
        assert db.get(SchedulerJobRun, run_id).status == "running"

    state["crash_on"] = None
    _expire_lease(task_id)
    assert run_once(task_id=task_id) == (task_id, "succeeded")
    assert calls == {"a": 1, "b": 2, "c": 1}, "completed steps are not re-run"
    finished = _task(task_id)
    assert finished.scheduler_job_run_id == run_id and finished.claim_token == 2
    with session_scope() as db:
        run = db.get(SchedulerJobRun, run_id)
        assert run.status == "succeeded" and run.steps_completed == ["a", "b", "c"]
        assert set(run.row_counts) == {"a", "b", "c"}


def test_hard_step_failure_fails_the_task_with_a_next_action(test_chain):
    chain_name, calls, state = test_chain
    state["fail"] = {"b"}
    task_id = _queue(chain_name)
    assert run_once(task_id=task_id) == (task_id, "failed")
    assert calls == {"a": 1, "b": 1, "c": 0}, "a hard failure still aborts later steps"
    task = _task(task_id)
    assert task.result["failed_step"] == "b" and f"govcon jobs run {chain_name}" in task.blocker_next_action
    assert task.checkpoint["completed_steps"] == ["a"], "a failed step is never checkpointed as done"
    with session_scope() as db:
        run = db.get(SchedulerJobRun, task.scheduler_job_run_id)
        assert (run.status, run.failed_step) == ("failed", "b")


def test_a_concurrent_run_in_the_same_lock_group_is_cancelled(test_chain):
    chain_name, calls, _ = test_chain
    task_id = _queue(chain_name)
    engine = make_engine()
    try:
        with engine.connect() as holder:
            holder.execute(text("SELECT pg_advisory_lock(742901, :key)"), {"key": chains._chain_lock_key(chain_name)})
            holder.commit()
            assert run_once(task_id=task_id) == (task_id, "cancelled")
            holder.execute(text("SELECT pg_advisory_unlock_all()"))
            holder.commit()
    finally:
        engine.dispose()
    assert calls == {"a": 0, "b": 0, "c": 0}
    assert "in progress" in _task(task_id).last_error


def test_scheduler_fire_queues_one_task_per_slot(monkeypatch):
    from govcon.scheduler import runner

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2030, 1, 1, 12, 0, 30, tzinfo=UTC)

    monkeypatch.setattr("datetime.datetime", Frozen)
    runner.execute_scheduled_chain("midday_check")
    runner.execute_scheduled_chain("midday_check")
    with session_scope() as db:
        tasks = db.query(Task).filter(Task.task_type == CHAIN_TASK,
                                      Task.input_revision["slot"].astext == "2030-01-01T12:00").all()
        assert len(tasks) == 1 and tasks[0].payload == {"chain_name": "midday_check", "trigger": "scheduler"}
        task_id = tasks[0].id
    monkeypatch.undo()
    assert run_once(task_id=task_id) == (task_id, "succeeded")
    task = _task(task_id)
    assert task.result["status"] == "succeeded" and task.result["steps_completed"] == ["midday_deadline_check", "review_escalations", "company_registration"]


def test_jobs_run_records_the_run_through_a_task():
    from typer.testing import CliRunner

    from govcon.cli import app
    result = CliRunner().invoke(app, ["jobs", "run", "midday_check"])
    assert result.exit_code == 0 and "status: succeeded" in result.output
    run_id = int(result.output.split("run_id: ")[1].split()[0])
    with session_scope() as db:
        task = db.query(Task).filter(Task.scheduler_job_run_id == run_id).one()
        assert task.task_type == CHAIN_TASK and task.status == "succeeded"
