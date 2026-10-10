"""Scheduler chains as durable, resumable tasks (ADR-063).

The scheduler queues a ``scheduler_chain`` task instead of running the chain
in its own process. A worker runs the chain step by step under the chain
group's advisory lock. Each step's own work and its checkpoint commit
together, so a worker that dies mid-chain is replaced by another that resumes
at the first unfinished step instead of re-running the chain blindly. The
``scheduler_job_runs`` row created by the first attempt stays the record of
the whole run.

Soft/hard step semantics, ``row_counts`` and ``/ops`` visibility are the same
as :func:`govcon.scheduler.chains.run_chain`, which remains for ``--inline``
runs.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from govcon.config import Settings
from govcon.db import make_engine, session_scope
from govcon.models import SchedulerJobRun, Task
from govcon.scheduler import chains
from govcon.scheduler.jobs import StepResult
from govcon.scheduler.limits import (
    PUBLISH_GRACE_SECONDS,
    run_step_bounded,
    step_timeout,
)
from govcon.tasks import queue

logger = logging.getLogger(__name__)

CHAIN_TASK = "scheduler_chain"


def queue_chain(session: Session, chain_name: str, *, trigger: str, slot: str,
                actor_user_id: int | None = None) -> tuple[Task, bool]:
    """Queue one run of ``chain_name``. The same ``slot`` is queued once while active."""
    if chain_name not in chains.CHAIN_DEFINITIONS:
        raise ValueError(f"Unknown chain: {chain_name!r}")
    return queue.enqueue(
        session,
        task_type=CHAIN_TASK,
        opportunity_id=None,
        input_revision={"chain": chain_name, "slot": slot},
        payload={"chain_name": chain_name, "trigger": trigger},
        actor_user_id=actor_user_id,
    )


def chain_task_failed(session: Session, task: Task) -> None:
    """A chain task that failed outside a step must not leave its run row "running"."""
    if task.scheduler_job_run_id is None:
        return
    run = session.get(SchedulerJobRun, task.scheduler_job_run_id)
    if run is not None and run.status == "running":
        run.status = "failed"
        run.finished_at = datetime.now(UTC)
        run.failed_step = run.failed_step or task.current_step
        run.error = task.last_error or "the chain task failed before finishing"


def _jsonable(result: StepResult) -> dict[str, Any]:
    return json.loads(json.dumps(asdict(result), default=str))


def chain_result_from_task(task: Task) -> chains.ChainResult | None:
    data = task.result or {}
    if not data:
        return None
    return chains.ChainResult(
        chain_name=data["chain_name"],
        run_id=data.get("run_id"),
        status=data["status"],
        steps_completed=list(data.get("steps_completed") or []),
        failed_step=data.get("failed_step"),
        error=data.get("error"),
        step_results=[StepResult(**r) for r in data.get("step_results") or []],
    )


def _start_run(settings: Settings, claim: queue.Claim, chain_name: str, trigger: str, lock_key: int) -> int:
    """Create the run row on the first attempt; fail stale rows from other runs."""
    with session_scope(settings) as db:
        task = queue.guard_publish(db, claim)
        task.current_step = task.current_step or "starting"
        related = [name for name in chains.CHAIN_DEFINITIONS if chains._chain_lock_key(name) == lock_key]
        for row in db.scalars(select(SchedulerJobRun).where(
            SchedulerJobRun.status == "running", SchedulerJobRun.chain_name.in_(related),
            SchedulerJobRun.id != (task.scheduler_job_run_id or -1),
        )):
            row.status = "failed"
            row.finished_at = datetime.now(UTC)
            row.error = "worker stopped before completion; recovered after advisory lock release"
        if task.scheduler_job_run_id is None:
            run = SchedulerJobRun(chain_name=chain_name, trigger=trigger,
                                  started_at=datetime.now(UTC), status="running", steps_completed=[])
            db.add(run)
            db.flush()
            task.scheduler_job_run_id = run.id
        return task.scheduler_job_run_id


def _record_step(db: Session, claim: queue.Claim, result: StepResult, *, done: bool) -> None:
    """Store a step's result in the step's own transaction (lease re-verified).

    Only ``done`` steps are skipped on resume; a hard failure is retried by
    a later run rather than treated as finished.
    """
    task = queue.guard_publish(db, claim)
    if done:
        queue.record_step(task, result.step)
    checkpoint = dict(task.checkpoint or {})
    data = dict(checkpoint.get("data") or {})
    results = dict(data.get("step_results") or {})
    results[result.step] = _jsonable(result)
    data["step_results"] = results
    checkpoint["data"] = data
    task.checkpoint = checkpoint


def run_chain_task(settings: Settings, claim: queue.Claim, heartbeat) -> str:
    with session_scope(settings) as db:
        task = queue.guard_publish(db, claim)
        payload = task.payload or {}
        raw_name = payload.get("chain_name")
        chain_name = raw_name if isinstance(raw_name, str) else None
        raw_trigger = payload.get("trigger")
        trigger = raw_trigger if isinstance(raw_trigger, str) and raw_trigger else "scheduler"
        completed = list((task.checkpoint or {}).get("completed_steps") or [])
    chain_def = chains.CHAIN_DEFINITIONS.get(chain_name) if isinstance(chain_name, str) else None
    if not isinstance(chain_name, str) or chain_def is None:
        with session_scope(settings) as db:
            queue.fail_terminal(db, queue.guard_publish(db, claim), f"unknown chain {chain_name!r}",
                                next_action="Remove the task; the chain no longer exists.", settings=settings)
        return "failed"

    lock_key = chains._chain_lock_key(chain_name)
    engine = make_engine(settings)
    try:
        with engine.connect() as lock_connection:
            acquired = lock_connection.scalar(text("SELECT pg_try_advisory_lock(742901, :key)"), {"key": lock_key})
            lock_connection.commit()
            if not acquired:
                from govcon.ops.reaper import reap_in_new_session

                # A holder past the chain maximum is stopped here, so this run can start.
                if reap_in_new_session(settings, actor=f"chain task {claim.task_id}"):
                    for _ in range(10):
                        acquired = lock_connection.scalar(
                            text("SELECT pg_try_advisory_lock(742901, :key)"), {"key": lock_key})
                        lock_connection.commit()
                        if acquired:
                            break
                        time.sleep(0.5)
            if not acquired:
                with session_scope(settings) as db:
                    queue.cancel(db, queue.guard_publish(db, claim),
                                 reason="another run of this chain group is in progress")
                return "cancelled"
            try:
                run_id = _start_run(settings, claim, chain_name, trigger, lock_key)
                return _run_steps(settings, claim, heartbeat, chain_def, run_id, completed, lock_connection)
            finally:
                lock_connection.execute(text("SELECT pg_advisory_unlock(742901, :key)"), {"key": lock_key})
                lock_connection.commit()
    finally:
        engine.dispose()


def _run_steps(settings, claim, heartbeat, chain_def, run_id, completed, lock_connection) -> str:
    failed_step: str | None = None
    chain_error: str | None = None
    for step_name in chain_def.steps:
        if step_name in completed:
            continue
        try:
            lock_connection.execute(text("SELECT 1"))
            lock_connection.commit()
        except Exception as exc:
            raise queue.LeaseLost(f"chain lock connection was lost: {exc}") from exc
        step_fn = chains._STEP_FUNCTIONS.get(step_name)
        if step_fn is None:
            failed_step, chain_error = step_name, f"No implementation for step '{step_name}'"
            break
        limit = step_timeout(step_name, settings)
        heartbeat.set_deadline(limit + PUBLISH_GRACE_SECONDS + 60)
        with session_scope(settings) as db:
            queue.guard_publish(db, claim).current_step = step_name
        logger.info("chain=%s step=%s starting (limit %ss)", chain_def.name, step_name, limit)
        soft = step_name in chain_def.soft_steps

        def publish(step_db: Session, result: StepResult, *, _soft: bool = soft, _step: str = step_name) -> None:
            if heartbeat.lost:
                raise queue.LeaseLost(f"task {claim.task_id}: lease lost during {_step}")
            _record_step(step_db, claim, result, done=_soft or not result.failed)

        try:
            result = run_step_bounded(step_name, step_fn, settings, timeout=limit, publish=publish)
            if result.extra.get("timed_out"):
                with session_scope(settings) as db:
                    _record_step(db, claim, result, done=soft)
        except queue.LeaseLost:
            raise
        except Exception as exc:
            logger.exception("chain=%s step=%s uncaught error", chain_def.name, step_name)
            result = StepResult(step=step_name, status="failed", error=str(exc))
            with session_scope(settings) as db:
                _record_step(db, claim, result, done=soft)
        if result.failed and not soft:
            failed_step, chain_error = step_name, result.error
            break
    return _finish(settings, claim, chain_def, run_id, failed_step, chain_error)


def _finish(settings, claim, chain_def, run_id, failed_step, chain_error) -> str:
    with session_scope(settings) as db:
        task = queue.guard_publish(db, claim)
        results = [StepResult(**r) for r in
                   ((task.checkpoint or {}).get("data") or {}).get("step_results", {}).values()]
        by_step = {r.step: r for r in results}
        ordered = [by_step[name] for name in chain_def.steps if name in by_step]
        steps_completed = [r.step for r in ordered if not r.failed]
        soft_errors = [f"{r.step}: {r.error}" for r in ordered
                       if (r.failed and r.step in chain_def.soft_steps) or r.status == "completed_with_errors"]
        if failed_step is not None:
            status = "failed"
        elif soft_errors:
            status, chain_error = "completed_with_errors", "; ".join(soft_errors)
        else:
            status = "succeeded"
        run = db.get(SchedulerJobRun, run_id)
        if run is not None:
            run.finished_at = datetime.now(UTC)
            run.status = status
            # JSONB column is annotated as a dict; the stored value is the step-name list.
            stored_steps: Any = steps_completed
            run.steps_completed = stored_steps
            run.failed_step = failed_step
            run.error = chain_error
            run.row_counts = {r.step: r.row_counts() for r in ordered}
        result = {
            "chain_name": chain_def.name, "run_id": run_id, "status": status,
            "steps_completed": steps_completed, "failed_step": failed_step, "error": chain_error,
            "step_results": [_jsonable(r) for r in ordered],
        }
        if status == "failed":
            # A hard step failure is a real outcome, not a crash: report it and
            # let a person decide, rather than re-running ingest automatically.
            task.result = result
            queue.fail_terminal(
                db, task, f"step {failed_step} failed: {chain_error}", settings=settings,
                next_action=f"Inspect the failed step on /ops, fix the cause, then run "
                            f"`govcon jobs run {chain_def.name}`.",
            )
        else:
            queue.complete(task, result)
        return task.status
