"""Audit round 2, finding 1: a task whose worker keeps dying is not re-run past ``max_attempts``.

A worker that dies (killed, out of memory, step over its timeout) never reaches
``retry_later``; its task stays ``running`` until the lease expires and is then
claimed again. These tests pin the current resume behaviour and prove the limit
also holds on that path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from govcon.db import session_scope
from govcon.models import AuditEvent, Task
from govcon.tasks import queue, registry
from govcon.tasks.registry import Step, TaskHandler
from govcon.tasks.worker import run_once

pytestmark = pytest.mark.usefixtures("upgraded_engine")


class _Crash(BaseException):
    """Stands in for the worker process dying: nothing in the worker catches it."""


@pytest.fixture()
def crashy(monkeypatch):
    """A one-step task type; ``state['mode']`` makes its step crash, raise, or succeed."""
    # tasks.task_type is a closed set; borrow one and swap in this handler.
    task_type = "solicitation_summary"
    state = {"mode": "ok", "runs": 0, "on_failed": []}

    def execute(inputs, ctx):
        state["runs"] += 1
        if state["mode"] == "crash":
            raise _Crash()
        if state["mode"] == "error":
            raise RuntimeError("step error")
        return inputs

    def publish(session, task, output, ctx):
        ctx.result = {"ok": True}

    def on_failed(session, task):
        state["on_failed"].append(task.id)

    registry.get_handler("ai_analysis")  # load built-ins before adding ours
    monkeypatch.setitem(registry._HANDLERS, task_type, TaskHandler(
        task_type=task_type, on_failed=on_failed,
        steps=[Step("only", prepare=lambda session, task, ctx: None, execute=execute, publish=publish)],
    ))
    return task_type, state


def _queue(task_type: str, max_attempts: int) -> int:
    with session_scope() as db:
        task, _ = queue.enqueue(db, task_type=task_type, opportunity_id=None,
                                input_revision={"n": uuid4().hex}, max_attempts=max_attempts)
        return task.id


def _task(task_id: int) -> Task:
    with session_scope() as db:
        return db.get(Task, task_id)


def _expire_lease(task_id: int) -> None:
    with session_scope() as db:
        db.execute(update(Task).where(Task.id == task_id).values(
            lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)))


def _crash(task_id: int, state: dict) -> None:
    state["mode"] = "crash"
    with pytest.raises(_Crash):
        run_once(task_id=task_id)
    _expire_lease(task_id)


def _audits(task_id: int, action: str) -> list[AuditEvent]:
    with session_scope() as db:
        return list(db.scalars(select(AuditEvent).where(
            AuditEvent.entity_type == "tasks", AuditEvent.entity_id == task_id, AuditEvent.action_type == action)))


# ── current behaviour that must not change ───────────────────────────────────

def test_crashed_task_with_attempts_left_is_resumed(crashy):
    task_type, state = crashy
    task_id = _queue(task_type, max_attempts=2)
    _crash(task_id, state)
    state["mode"] = "ok"
    assert run_once(task_id=task_id) == (task_id, "succeeded")
    task = _task(task_id)
    assert (task.attempts, task.claim_token, task.result) == (2, 2, {"ok": True})
    assert state["runs"] == 2 and state["on_failed"] == []


def test_running_task_with_an_unexpired_lease_is_not_claimed(crashy):
    task_type, state = crashy
    task_id = _queue(task_type, max_attempts=2)
    state["mode"] = "crash"
    with pytest.raises(_Crash):
        run_once(task_id=task_id)
    assert run_once(task_id=task_id) is None
    _expire_lease(task_id)
    state["mode"] = "ok"
    assert run_once(task_id=task_id) == (task_id, "succeeded")


def test_step_error_on_the_last_attempt_fails_and_calls_on_failed(crashy):
    task_type, state = crashy
    task_id = _queue(task_type, max_attempts=1)
    state["mode"] = "error"
    assert run_once(task_id=task_id) == (task_id, "failed")
    task = _task(task_id)
    assert (task.status, task.attempts, task.last_error_type) == ("failed", 1, "RuntimeError")
    assert task.blocker_owner_role == "owner" and task.blocker_next_action
    assert state["on_failed"] == [task_id] and len(_audits(task_id, "task_failed")) == 1


def test_step_error_with_attempts_left_is_retried(crashy):
    task_type, state = crashy
    task_id = _queue(task_type, max_attempts=2)
    state["mode"] = "error"
    assert run_once(task_id=task_id) == (task_id, "retrying")
    with session_scope() as db:
        db.execute(update(Task).where(Task.id == task_id).values(next_attempt_at=datetime.now(UTC)))
    state["mode"] = "ok"
    assert run_once(task_id=task_id) == (task_id, "succeeded")
    assert _task(task_id).attempts == 2


# ── the finding ──────────────────────────────────────────────────────────────

def test_task_whose_worker_died_on_its_last_attempt_fails_instead_of_running_again(crashy):
    task_type, state = crashy
    task_id = _queue(task_type, max_attempts=2)
    _crash(task_id, state)  # attempt 1
    _crash(task_id, state)  # attempt 2 (the last allowed)
    assert state["runs"] == 2
    state["mode"] = "ok"

    assert run_once(task_id=task_id) == (task_id, "failed")
    assert state["runs"] == 2, "a task past max_attempts must not run again"
    task = _task(task_id)
    assert task.status == "failed" and task.lease_owner is None and task.finished_at is not None
    assert task.blocker_owner_role == "owner" and "retry" in task.blocker_next_action
    assert "attempt 2 of 2" in task.last_error
    assert state["on_failed"] == [task_id]
    assert len(_audits(task_id, "task_failed")) == 1
    _expire_lease(task_id)
    assert run_once(task_id=task_id) is None, "a failed task is never claimed again"


def test_failed_after_crashes_can_still_be_requeued_by_a_person(crashy):
    task_type, state = crashy
    task_id = _queue(task_type, max_attempts=1)
    _crash(task_id, state)
    assert run_once(task_id=task_id) == (task_id, "failed")
    with session_scope() as db:
        queue.requeue(db, db.get(Task, task_id), actor_user_id=None, reason="fixed the worker")
    state["mode"] = "ok"
    assert run_once(task_id=task_id) == (task_id, "succeeded")
