"""Mark scheduler runs that can no longer finish as failed, with the reason.

A ``scheduler_job_runs`` row stays ``running`` forever if its worker was
killed, the laptop slept through the run, or a step hung while holding the
chain group's advisory lock (every later run of the group is then refused the
lock and cancelled). The reaper runs at worker and scheduler startup and every
few minutes after that. A row is reaped only when:

* no session holds the chain group's advisory lock (the worker is gone), or
* it has run past the chain's maximum, the sum of its hard step limits plus
  slack. The backend still holding that lock is terminated so the next run
  can start; the hung worker's late publish is then refused by the lease fence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from govcon.config import Settings
from govcon.models import TERMINAL_TASK_STATUSES, IngestionRun, SchedulerJobRun, Task

logger = logging.getLogger(__name__)

CHAIN_LOCK_CLASS = 742901
# A row this young may belong to a run that has just taken its lock.
NO_LOCK_GRACE = timedelta(minutes=2)


class StaleRunReaped(RuntimeError):
    """The run's worker stopped or the run passed its maximum duration."""


@dataclass
class ReapedRun:
    run_id: int
    chain_name: str
    reason: str


def held_chain_locks(session: Session) -> set[int]:
    """Advisory lock keys of chain groups that some session holds right now."""
    rows = session.execute(text(
        "SELECT objid FROM pg_locks WHERE locktype = 'advisory' AND granted "
        "AND classid = :cls AND objsubid = 2 "
        "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
    ), {"cls": CHAIN_LOCK_CLASS}).scalars()
    return {int(value) for value in rows}


def _terminate_lock_holders(session: Session, key: int) -> int:
    pids = session.execute(text(
        "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted "
        "AND classid = :cls AND objid = :key AND objsubid = 2 AND pid <> pg_backend_pid() "
        "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
    ), {"cls": CHAIN_LOCK_CLASS, "key": key}).scalars().all()
    for pid in pids:
        session.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
    return len(pids)


def _duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 120:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60} min"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def reap_stale_runs(session: Session, settings: Settings, *, now: datetime | None = None) -> list[ReapedRun]:
    """Fail every stale ``running`` chain and ingestion row. Returns what was reaped."""
    from govcon.scheduler.chains import CHAIN_DEFINITIONS, _chain_lock_key
    from govcon.scheduler.limits import chain_max_seconds, step_timeout
    from govcon.tasks import queue

    now = now or datetime.now(UTC)
    # A live worker publishing a step holds its task row only briefly.
    session.execute(text("SELECT set_config('lock_timeout', '10s', true)"))
    held = held_chain_locks(session)
    reaped: list[ReapedRun] = []
    running = session.scalars(
        select(SchedulerJobRun).where(SchedulerJobRun.status == "running")
        .order_by(SchedulerJobRun.id).with_for_update(skip_locked=True)
    ).all()
    for run in running:
        known = run.chain_name in CHAIN_DEFINITIONS
        key = _chain_lock_key(run.chain_name) if known else None
        elapsed = (now - _aware(run.started_at)).total_seconds()
        limit = chain_max_seconds(run.chain_name, settings)
        tasks = session.scalars(select(Task).where(
            Task.scheduler_job_run_id == run.id, Task.status.notin_(TERMINAL_TASK_STATUSES),
        ).with_for_update()).all()
        step = run.failed_step or next((t.current_step for t in tasks if t.current_step), None)
        if elapsed > limit:
            reason = (f"ran {_duration(elapsed)}, past this chain's maximum of {_duration(limit)}"
                      f"{f' (stuck in step {step})' if step and step != 'starting' else ''}; "
                      "marked failed by the stale-run check")
            if key is not None and key in held:
                stopped = _terminate_lock_holders(session, key)
                if stopped:
                    reason += f"; stopped {stopped} database session(s) still holding the chain lock"
        elif key is not None and key not in held and elapsed > NO_LOCK_GRACE.total_seconds():
            if any(task.attempts < task.max_attempts for task in tasks):
                # The queue hands the expired lease to the next worker, which resumes this run.
                continue
            reason = ("the worker running this chain stopped before it finished (no process holds "
                      "the chain lock); marked failed by the stale-run check")
        else:
            continue
        run.status = "failed"
        run.finished_at = now
        run.failed_step = step if step and step != "starting" else run.failed_step
        run.error = reason
        for task in tasks:
            queue.fail_terminal(
                session, task, StaleRunReaped(reason), settings=settings,
                next_action=f"Run {run.chain_name} again from /ops once the cause in the worker log is fixed.",
            )
        reaped.append(ReapedRun(run.id, run.chain_name, reason))
        logger.warning("reaped scheduler run %s (%s): %s", run.id, run.chain_name, reason)

    longest = max(step_timeout(name, settings) for chain in CHAIN_DEFINITIONS.values() for name in chain.steps)
    cutoff = now - timedelta(seconds=longest * 2)
    for ingest in session.scalars(select(IngestionRun).where(
        IngestionRun.status == "running", IngestionRun.finished_at.is_(None), IngestionRun.started_at < cutoff,
    ).with_for_update(skip_locked=True)):
        ingest.status = "failed"
        ingest.finished_at = now
        errors = dict(ingest.errors or {})
        errors["messages"] = [*list(errors.get("messages") or []),
                              "never finished; marked failed by the stale-run check"]
        ingest.errors = errors
    session.flush()
    return reaped


def reap_in_new_session(settings: Settings, *, actor: str) -> list[ReapedRun]:
    """Best effort for startup and periodic loops: a database problem is logged, not raised."""
    from govcon.db import session_scope

    try:
        with session_scope(settings) as db:
            reaped = reap_stale_runs(db, settings)
    except Exception:
        logger.exception("%s: stale-run check failed", actor)
        return []
    if reaped:
        logger.warning("%s: marked %d stale scheduler run(s) failed", actor, len(reaped))
    return reaped
