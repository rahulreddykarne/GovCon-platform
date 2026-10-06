"""Worker loop: claim a task, run its remaining steps, publish under lease.

Started with ``govcon worker start``. Any number of workers may run against
one database; ``SKIP LOCKED`` hands each task to one of them. A heartbeat
thread renews the lease while a step is inside its timeout. A worker that
crashes, hangs or overruns stops renewing, so its lease expires, another
worker claims the task, and the stale worker's publish is refused.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

from govcon.config import Settings, get_settings
from govcon.db import session_scope
from govcon.models import Task
from govcon.tasks import queue
from govcon.tasks.errors import TaskFailedPermanently, TaskSuperseded, classify
from govcon.tasks.registry import StepContext, get_handler

logger = logging.getLogger(__name__)


def new_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class _Heartbeat(threading.Thread):
    """Renews the lease every third of its length until stopped or past the step deadline."""

    def __init__(self, settings: Settings, claim: queue.Claim) -> None:
        super().__init__(name=f"task-{claim.task_id}-heartbeat", daemon=True)
        self._settings = settings
        self._claim = claim
        self._stop_event = threading.Event()
        self._deadline: datetime | None = None
        self.lost = False

    def set_deadline(self, seconds: int) -> None:
        self._deadline = datetime.now(UTC) + timedelta(seconds=seconds)

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        lease = self._settings.task_lease_seconds
        interval = max(0.2, lease / 3)
        while not self._stop_event.wait(interval):
            if self._deadline is not None and datetime.now(UTC) > self._deadline:
                logger.warning("task %s: step exceeded its timeout; lease will lapse", self._claim.task_id)
                return
            try:
                with session_scope(self._settings) as db:
                    renewed = queue.renew_lease(db, self._claim, lease)
            except Exception:  # transient DB trouble: try again next tick
                logger.exception("task %s: lease renewal failed", self._claim.task_id)
                continue
            if not renewed:
                self.lost = True
                logger.warning("task %s: lease lost", self._claim.task_id)
                return


def _context(task: Task, settings: Settings) -> StepContext:
    return StepContext(
        settings=settings,
        task_id=task.id,
        opportunity_id=task.opportunity_id,
        payload=dict(task.payload or {}),
        input_revision=dict(task.input_revision or {}),
    )


def _record_outcome(settings: Settings, claim: queue.Claim, exc: BaseException) -> str:
    """Store the outcome of a failed step; returns the task's new status."""
    outcome = classify(exc)
    try:
        with session_scope(settings) as db:
            task = queue.guard_publish(db, claim)
            if outcome.kind == "block":
                assert outcome.status is not None and outcome.owner_role is not None and outcome.next_action is not None
                queue.block(db, task, status=outcome.status, reason=str(exc),
                            owner_role=outcome.owner_role, next_action=outcome.next_action,
                            resume_at=outcome.resume_at, settings=settings)
            elif outcome.kind == "cancel":
                queue.cancel(db, task, reason=str(exc), superseded_by=outcome.superseded_by)
            elif outcome.kind == "supersede":
                assert isinstance(exc, TaskSuperseded)
                # Cancel first: the replacement may share no key, but the
                # active-task index must never see both as active.
                queue.cancel(db, task, reason=str(exc))
                replacement_payload = exc.payload if exc.payload is not None else dict(task.payload or {})
                replacement, _ = queue.enqueue(
                    db, task_type=task.task_type, opportunity_id=task.opportunity_id,
                    input_revision=exc.input_revision,
                    payload=replacement_payload,
                    actor_user_id=task.created_by_user_id, settings=settings,
                )
                task.superseded_by_task_id = replacement.id
            elif outcome.kind == "fail":
                assert outcome.owner_role is not None
                queue.fail_terminal(db, task, exc, owner_role=outcome.owner_role,
                                    next_action=outcome.next_action, settings=settings)
            else:
                queue.retry_later(db, task, exc, settings=settings)
            if task.status == "failed":
                handler = get_handler(task.task_type)
                if handler.on_failed is not None:
                    handler.on_failed(db, task)
            return task.status
    except queue.LeaseLost:
        logger.warning("task %s: lease lost before recording %s", claim.task_id, type(exc).__name__)
        return "lease_lost"


def run_claimed(settings: Settings, claim: queue.Claim) -> str:
    """Run every remaining step of a claimed task. Returns the final status."""
    handler = get_handler(claim.task_type)
    heartbeat = _Heartbeat(settings, claim)
    heartbeat.start()
    try:
        if handler.run is not None:
            try:
                return handler.run(settings, claim, heartbeat)
            except queue.LeaseLost as exc:
                logger.warning("%s; discarding this worker's result", exc)
                return "lease_lost"
            except Exception as exc:  # noqa: BLE001  boundary must record any failure
                logger.warning("task %s failed: %s", claim.task_id, type(exc).__name__)
                return _record_outcome(settings, claim, exc)
        for index, step in enumerate(handler.steps):
            last = index == len(handler.steps) - 1
            heartbeat.set_deadline(step.timeout_seconds)
            try:
                # 1. Prepare: short transaction under the locks.
                with session_scope(settings) as db:
                    task = queue.guard_publish(db, claim)
                    if step.name in (task.checkpoint or {}).get("completed_steps", []):
                        continue
                    task.current_step = step.name
                    ctx = _context(task, settings)
                    inputs = step.prepare(db, task, ctx)
                # 2. Execute: no transaction open.
                output = step.execute(inputs, ctx) if step.execute is not None else inputs
                if heartbeat.lost:
                    raise queue.LeaseLost(f"task {claim.task_id}: lease lost during {step.name}")
                # 3. Publish: recheck lease and inputs, write, checkpoint — one commit.
                with session_scope(settings) as db:
                    task = queue.guard_publish(db, claim)
                    step.publish(db, task, output, ctx)
                    if task.status == "running":
                        queue.record_step(task, step.name, ctx.checkpoint_data)
                        if last:
                            queue.complete(task, ctx.result)
                    status = task.status
                if status != "running":
                    return status
            except queue.LeaseLost as exc:
                logger.warning("%s; discarding this worker's result", exc)
                return "lease_lost"
            except Exception as exc:  # noqa: BLE001  boundary must record any failure
                logger.warning("task %s step %s failed: %s", claim.task_id, step.name, type(exc).__name__)
                return _record_outcome(settings, claim, exc)
        # Every step was already checkpointed (resumed after the final publish).
        with session_scope(settings) as db:
            task = queue.guard_publish(db, claim)
            queue.complete(task, task.result)
            return task.status
    except queue.LeaseLost:
        return "lease_lost"
    finally:
        heartbeat.stop()


def run_once(
    settings: Settings | None = None,
    *,
    worker_id: str | None = None,
    task_types: list[str] | None = None,
    task_id: int | None = None,
    opportunity_id: int | None = None,
) -> tuple[int, str] | None:
    """Claim and run one task. Returns ``(task_id, status)`` or None when idle."""
    settings = settings or get_settings()
    worker_id = worker_id or new_worker_id()
    with session_scope(settings) as db:
        claim = queue.claim(db, worker_id=worker_id, lease_seconds=settings.task_lease_seconds,
                            task_types=task_types, task_id=task_id, opportunity_id=opportunity_id,
                            settings=settings)
    if claim is None:
        return None
    logger.info("task %s (%s) claimed by %s", claim.task_id, claim.task_type, worker_id)
    with session_scope(settings) as db:
        task = db.get(Task, claim.task_id)
        exhausted = task is not None and task.attempts > task.max_attempts
        max_attempts = task.max_attempts if task is not None else 0
    if exhausted:
        # Only an expired lease is claimed past the limit: the worker died
        # (killed, out of memory, step over its timeout) on its last attempt.
        status = _record_outcome(settings, claim, TaskFailedPermanently(
            f"the worker stopped during attempt {max_attempts} of {max_attempts} (killed, out of memory, "
            "or a step over its timeout); not run again",
            owner_role="owner",
            next_action="Check the worker log for why it stopped, fix the cause, then retry the task.",
        ))
    else:
        status = run_claimed(settings, claim)
    logger.info("task %s finished with %s", claim.task_id, status)
    return claim.task_id, status


def run_worker(
    settings: Settings | None = None,
    *,
    task_types: list[str] | None = None,
    until_idle: bool = False,
    max_tasks: int | None = None,
    stop_event: threading.Event | None = None,
) -> int:
    """Process tasks until stopped (or until none are due, with ``until_idle``)."""
    settings = settings or get_settings()
    worker_id = new_worker_id()
    stop_event = stop_event or threading.Event()
    processed = 0
    logger.info("worker %s started (types=%s)", worker_id, task_types or "all")
    while not stop_event.is_set():
        _beat(settings, "worker", worker_id)
        if max_tasks is not None and processed >= max_tasks:
            break
        try:
            ran = run_once(settings, worker_id=worker_id, task_types=task_types)
        except Exception:
            if until_idle:
                raise
            # Transient trouble (database restart, dropped connection): keep the
            # worker alive; an interrupted task's lease lapses and it is re-claimed.
            logger.exception("worker %s: run failed; retrying after %ss", worker_id, settings.worker_poll_seconds)
            stop_event.wait(settings.worker_poll_seconds)
            continue
        if ran is None:
            if until_idle:
                break
            stop_event.wait(settings.worker_poll_seconds)
            continue
        processed += 1
    logger.info("worker %s stopped after %d task(s)", worker_id, processed)
    return processed


def _beat(settings: Settings, role: str, instance_id: str) -> None:
    """Record liveness. A database problem here must not kill the worker."""
    try:
        from govcon.ops.health import record_heartbeat

        with session_scope(settings) as db:
            record_heartbeat(db, role=role, instance_id=instance_id)
    except Exception:
        logger.warning("%s heartbeat was not recorded", role)


def wait_for(settings: Settings, task_id: int, *, timeout: float, poll: float = 1.0) -> Task | None:
    """Poll until the task leaves the running/queued states or the timeout passes."""
    deadline = time.monotonic() + timeout
    while True:
        with session_scope(settings) as db:
            task = db.get(Task, task_id)
            if task is None or task.status not in ("queued", "running", "retrying"):
                return task
        if time.monotonic() > deadline:
            return task
        time.sleep(poll)
