"""Durable task queue on PostgreSQL (ADR-061).

Business code calls :func:`enqueue` inside its own transaction, so the change
and the work it implies commit together. Workers claim tasks with
``FOR UPDATE SKIP LOCKED`` in a short transaction, run each step's external
work with no transaction open, then publish under :func:`guard_publish`, which
locks the opportunity first (the workflow locking convention), then the task,
and refuses a worker whose lease expired or was taken over.

``claim_token`` is the fencing token: every claim increments it and nothing
decrements it, so a worker that lost its lease can never publish over its
successor. ``attempts`` counts attempts against ``max_attempts``.

Per-opportunity AI budgets are lifetime totals, so waiting does not refill
them. A ``waiting_for_budget`` task is retried automatically once, in case
another task's reservation was still settling, and then stays parked until
the configured limits change (``checkpoint["budget_block"]`` records them) or
a person re-queues it.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from sqlalchemy import select, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from govcon.audit import record_audit
from govcon.config import Settings, get_settings
from govcon.logging import redact
from govcon.models import TERMINAL_TASK_STATUSES, Task
from govcon.workflow.invalidation import lock_one, lock_opportunity

ACTIVE_STATUSES = ("queued", "running", "waiting_for_input", "waiting_for_budget", "retrying")
BLOCKED_STATUSES = ("waiting_for_input", "waiting_for_budget")
_ERROR_LIMIT = 500


class LeaseLost(RuntimeError):
    """The worker no longer holds the task's lease; its result must be discarded."""


@dataclass(frozen=True)
class Claim:
    """What a worker holds after claiming: the task id and its fencing token."""

    task_id: int
    task_type: str
    opportunity_id: int | None
    token: int
    worker_id: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def dedup_key(task_type: str, opportunity_id: int | None, input_revision: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"type": task_type, "opportunity_id": opportunity_id, "inputs": input_revision},
        sort_keys=True, default=str, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def enqueue(
    session: Session,
    *,
    task_type: str,
    opportunity_id: int | None,
    input_revision: dict[str, Any],
    payload: dict[str, Any] | None = None,
    actor_user_id: int | None = None,
    max_attempts: int | None = None,
    scheduler_job_run_id: int | None = None,
    settings: Settings | None = None,
) -> tuple[Task, bool]:
    """Queue work in the caller's transaction; return ``(task, created)``.

    Identical work (same type, opportunity and inputs) that is still active is
    returned instead of queued twice.
    """
    settings = settings or get_settings()
    key = dedup_key(task_type, opportunity_id, input_revision)
    values = {
        "task_type": task_type,
        "opportunity_id": opportunity_id,
        "dedup_key": key,
        "payload": payload or {},
        "input_revision": input_revision,
        "max_attempts": max_attempts or settings.task_default_max_attempts,
        "created_by_user_id": actor_user_id,
        "scheduler_job_run_id": scheduler_job_run_id,
    }
    session.flush()
    stmt = (
        insert(Task)
        .values(**values)
        .on_conflict_do_nothing(
            index_elements=["dedup_key"],
            index_where=text("status NOT IN ('succeeded','failed','cancelled')"),
        )
        .returning(Task.id)
    )
    new_id = session.scalar(stmt)
    if new_id is not None:
        return session.get(Task, new_id, populate_existing=True), True
    existing = session.scalar(
        select(Task).where(Task.dedup_key == key, Task.status.not_in(TERMINAL_TASK_STATUSES))
    )
    if existing is None:  # finished between the insert and this read; queue afresh
        return enqueue(
            session, task_type=task_type, opportunity_id=opportunity_id, input_revision=input_revision,
            payload=payload, actor_user_id=actor_user_id, max_attempts=max_attempts,
            scheduler_job_run_id=scheduler_job_run_id, settings=settings,
        )
    return existing, False


def active_task(session: Session, *, task_type: str, opportunity_id: int) -> Task | None:
    return session.scalar(
        select(Task)
        .where(Task.task_type == task_type, Task.opportunity_id == opportunity_id,
               Task.status.in_(ACTIVE_STATUSES))
        .order_by(Task.id.desc())
        .limit(1)
    )


def latest_task(session: Session, *, task_type: str, opportunity_id: int) -> Task | None:
    return session.scalar(
        select(Task)
        .where(Task.task_type == task_type, Task.opportunity_id == opportunity_id)
        .order_by(Task.id.desc())
        .limit(1)
    )


def claim(
    session: Session,
    *,
    worker_id: str,
    lease_seconds: int,
    task_types: list[str] | None = None,
    task_id: int | None = None,
    opportunity_id: int | None = None,
    settings: Settings | None = None,
) -> Claim | None:
    """Claim one due task, or one whose lease expired. Commit right after.

    A budget-blocked task is due only for its one automatic retry, or once
    the budget limits differ from those it was blocked under.
    """
    from govcon.ai.budget import budget_fingerprint

    settings = settings or get_settings()
    type_filter = "AND task_type = ANY(:types)" if task_types else ""
    id_filter = "AND id = :task_id" if task_id is not None else ""
    opp_filter = "AND opportunity_id = :opp_id" if opportunity_id is not None else ""
    row = session.execute(
        text(
            f"""
            UPDATE tasks SET
                status = 'running',
                attempts = attempts + 1,
                claim_token = claim_token + 1,
                lease_owner = :worker,
                lease_expires_at = now() + make_interval(secs => :lease),
                started_at = COALESCE(started_at, now())
            WHERE id = (
                SELECT id FROM tasks
                WHERE ((status IN ('queued', 'retrying') AND next_attempt_at <= now())
                       OR (status = 'waiting_for_budget' AND next_attempt_at <= now()
                           AND (COALESCE((checkpoint->'budget_block'->>'auto_retry')::boolean, true)
                                OR checkpoint->'budget_block'->>'fingerprint' IS DISTINCT FROM :budget_fp))
                       OR (status = 'running' AND lease_expires_at < now()))
                  {type_filter} {id_filter} {opp_filter}
                ORDER BY next_attempt_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING id, task_type, opportunity_id, claim_token
            """
        ),
        {"worker": worker_id, "lease": lease_seconds, "types": task_types or [], "task_id": task_id,
         "opp_id": opportunity_id, "budget_fp": budget_fingerprint(settings)},
    ).first()
    if row is None:
        return None
    return Claim(task_id=row.id, task_type=row.task_type, opportunity_id=row.opportunity_id,
                 token=row.claim_token, worker_id=worker_id)


def live_worker_hosts(session: Session) -> set[str]:
    """Hosts whose workers hold an unexpired lease (``lease_owner`` is host:pid:id)."""
    owners = session.scalars(
        select(Task.lease_owner).where(Task.status == "running", Task.lease_expires_at > text("now()"))
    ).all()
    return {owner.split(":", 1)[0] for owner in owners if owner}


def renew_lease(session: Session, claim_: Claim, lease_seconds: int) -> bool:
    """Extend the lease while the worker still owns it. Commit right after."""
    result = session.execute(
        text(
            """
            UPDATE tasks SET lease_expires_at = now() + make_interval(secs => :lease)
            WHERE id = :id AND status = 'running' AND lease_owner = :worker
              AND claim_token = :token AND lease_expires_at > now()
            """
        ),
        {"id": claim_.task_id, "lease": lease_seconds, "worker": claim_.worker_id, "token": claim_.token},
    )
    return cast(CursorResult[Any], result).rowcount == 1


def guard_publish(session: Session, claim_: Claim) -> Task:
    """Lock the opportunity, then the task, and verify the lease is still ours."""
    if claim_.opportunity_id is not None:
        lock_opportunity(session, claim_.opportunity_id)
    task = lock_one(session, select(Task).where(Task.id == claim_.task_id))
    if (
        task is None
        or task.status != "running"
        or task.lease_owner != claim_.worker_id
        or task.claim_token != claim_.token
        or task.lease_expires_at is None
        or task.lease_expires_at <= session.scalar(select(text("now()")))
    ):
        raise LeaseLost(f"task {claim_.task_id}: lease lost (claim {claim_.token})")
    return task


def record_step(task: Task, step: str, data: dict[str, Any] | None = None) -> None:
    """Mark ``step`` complete in the checkpoint; ``data`` is kept for later steps."""
    checkpoint = dict(task.checkpoint or {})
    done = list(checkpoint.get("completed_steps") or [])
    if step not in done:
        done.append(step)
    checkpoint["completed_steps"] = done
    if data:
        steps_data = dict(checkpoint.get("data") or {})
        steps_data[step] = data
        checkpoint["data"] = steps_data
    task.checkpoint = checkpoint


def _release(task: Task) -> None:
    task.lease_owner = None
    task.lease_expires_at = None


def _clear_blocker(task: Task) -> None:
    task.blocker_owner_role = None
    task.blocker_owner_user_id = None
    task.blocker_next_action = None


def clean_error(exc: BaseException | str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    message = exc if isinstance(exc, str) else (str(exc) or exc.__class__.__name__)
    return redact(message, settings.secret_values())[:_ERROR_LIMIT]


def complete(task: Task, result: dict[str, Any] | None = None) -> None:
    task.status = "succeeded"
    task.result = result
    task.current_step = None
    task.finished_at = _now()
    _release(task)
    _clear_blocker(task)


def retry_later(session: Session, task: Task, exc: BaseException, *, settings: Settings | None = None) -> None:
    """Schedule another attempt with capped exponential backoff, or fail terminally."""
    settings = settings or get_settings()
    task.last_error_type = exc.__class__.__name__
    task.last_error = clean_error(exc, settings)
    if task.attempts >= task.max_attempts:
        fail_terminal(session, task, exc, settings=settings)
        return
    base = settings.task_retry_base_seconds * 2 ** max(0, task.attempts - 1)
    delay = min(settings.task_retry_max_seconds, base) * (0.8 + 0.4 * random.random())
    task.status = "retrying"
    task.next_attempt_at = _now() + timedelta(seconds=delay)
    _release(task)


def fail_terminal(
    session: Session,
    task: Task,
    exc: BaseException | str,
    *,
    owner_role: str = "owner",
    next_action: str | None = None,
    settings: Settings | None = None,
) -> None:
    task.status = "failed"
    task.last_error_type = exc if isinstance(exc, str) else exc.__class__.__name__
    task.last_error = clean_error(exc, settings)
    task.blocker_owner_role = owner_role
    task.blocker_next_action = next_action or "Review the error, fix the cause, then retry the task."
    task.finished_at = _now()
    _release(task)
    record_audit(
        session, action_type="task_failed", opportunity_id=task.opportunity_id,
        entity_type="tasks", entity_id=task.id,
        new_value={"task_type": task.task_type, "attempts": task.attempts,
                   "error_type": task.last_error_type, "error": task.last_error},
    )


def block(
    session: Session,
    task: Task,
    *,
    status: str,
    reason: str,
    owner_role: str,
    next_action: str,
    owner_user_id: int | None = None,
    resume_at: datetime | None = None,
    settings: Settings | None = None,
) -> None:
    """Park the task until a person supplies input or budget becomes available."""
    if status not in BLOCKED_STATUSES:
        raise ValueError(f"not a blocked status: {status}")
    cleaned = clean_error(reason, settings)
    repeated = task.last_error_type == status and task.last_error == cleaned
    if status == "waiting_for_budget":
        _record_budget_block(task, settings or get_settings())
    task.status = status
    task.last_error_type = status
    task.last_error = cleaned
    task.blocker_owner_role = owner_role
    task.blocker_owner_user_id = owner_user_id
    task.blocker_next_action = next_action
    # A budget block is retried once after ``next_attempt_at``, then only when
    # the limits change (see ``claim``); an input block waits for a person.
    task.next_attempt_at = resume_at or (_now() + timedelta(hours=1))
    # A block is not a failed attempt; give back the claim's attempt.
    task.attempts = max(0, task.attempts - 1)
    _release(task)
    if repeated:
        return  # the same block again: already audited
    record_audit(
        session, action_type="task_blocked", opportunity_id=task.opportunity_id,
        entity_type="tasks", entity_id=task.id,
        new_value={"task_type": task.task_type, "status": status, "reason": task.last_error,
                   "owner_role": owner_role, "next_action": next_action},
    )


def _record_budget_block(task: Task, settings: Settings) -> None:
    """Allow one automatic retry per budget limits and per amount of progress."""
    from govcon.ai.budget import budget_fingerprint

    fingerprint = budget_fingerprint(settings)
    steps_done = len((task.checkpoint or {}).get("completed_steps") or [])
    previous = (task.checkpoint or {}).get("budget_block") or {}
    first = previous.get("fingerprint") != fingerprint or previous.get("steps_done") != steps_done
    task.checkpoint = {**(task.checkpoint or {}),
                       "budget_block": {"fingerprint": fingerprint, "steps_done": steps_done, "auto_retry": first}}


def budget_parked(task: Task) -> bool:
    """True when a budget-blocked task will not be retried until limits change or a person acts."""
    block_ = (task.checkpoint or {}).get("budget_block") or {}
    return task.status == "waiting_for_budget" and block_.get("auto_retry") is False


def cancel(session: Session, task: Task, *, reason: str, superseded_by: int | None = None,
           actor_user_id: int | None = None) -> None:
    task.status = "cancelled"
    task.last_error_type = "cancelled"
    task.last_error = clean_error(reason)
    task.superseded_by_task_id = superseded_by
    task.finished_at = _now()
    _release(task)
    record_audit(
        session, action_type="task_cancelled", user_id=actor_user_id, opportunity_id=task.opportunity_id,
        entity_type="tasks", entity_id=task.id,
        new_value={"task_type": task.task_type, "reason": task.last_error, "superseded_by": superseded_by},
    )


def cancel_active(session: Session, *, opportunity_id: int, task_types: tuple[str, ...], reason: str) -> int:
    """Cancel an opportunity's active tasks of the given types; the caller holds its lock."""
    rows = session.scalars(
        select(Task)
        .where(Task.opportunity_id == opportunity_id, Task.task_type.in_(task_types),
               Task.status.in_(ACTIVE_STATUSES))
        .with_for_update()
    ).all()
    for task in rows:
        cancel(session, task, reason=reason)
    return len(rows)


def requeue(session: Session, task: Task, *, actor_user_id: int | None, reason: str) -> Task:
    """Put a failed or input-blocked task back in the queue for a person."""
    if task.status not in ("failed", "waiting_for_input", "waiting_for_budget"):
        raise ValueError(f"task {task.id} is {task.status}; only failed or waiting tasks can be re-queued")
    if task.status == "failed":
        clash = session.scalar(
            select(Task.id).where(Task.dedup_key == task.dedup_key, Task.id != task.id,
                                  Task.status.in_(ACTIVE_STATUSES))
        )
        if clash is not None:
            raise ValueError(f"task {clash} already runs this work")
        task.attempts = 0
        task.finished_at = None
    task.status = "queued"
    task.next_attempt_at = _now()
    _clear_blocker(task)
    record_audit(
        session, action_type="task_requeued", user_id=actor_user_id, opportunity_id=task.opportunity_id,
        entity_type="tasks", entity_id=task.id,
        new_value={"task_type": task.task_type, "reason": reason},
    )
    return task
