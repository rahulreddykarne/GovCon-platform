"""Header status strip: last source pulls, running/stuck jobs, process heartbeats.

Every item is read from a stored row. A missing row is shown as missing, never
invented. The strip is safe to render on every page: it does not call SAM,
DIBBS, USAspending, or a model provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.models import IngestionRun, ProcessHeartbeat, SchedulerJobRun, Task
from govcon.ops.health import _SOURCE_JOBS, STALE_PROCESS

TASK_MAX_RUNTIME = timedelta(minutes=30)
CHAIN_MAX_RUNTIME = timedelta(hours=4)
ACTIVE_TASKS = ("queued", "running", "retrying")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _ago(when: datetime | None, now: datetime) -> str:
    if when is None:
        return "never"
    seconds = int((now - _aware(when)).total_seconds())
    if seconds < 90:
        return f"{max(seconds, 0)}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


@dataclass
class StripItem:
    key: str
    label: str
    text: str
    tone: str  # ok, warn, danger, missing
    stuck: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "text": self.text, "tone": self.tone, "stuck": self.stuck}


def _last_success(session: Session, jobs: tuple[str, ...]) -> IngestionRun | None:
    return session.scalar(
        select(IngestionRun)
        .where(IngestionRun.job.in_(jobs), IngestionRun.status.in_(("succeeded", "completed_with_errors")))
        .order_by(IngestionRun.finished_at.desc().nulls_last(), IngestionRun.started_at.desc())
        .limit(1)
    )


def _source_item(session: Session, key: str, label: str, now: datetime) -> StripItem:
    run = _last_success(session, _SOURCE_JOBS[key])
    if run is None:
        return StripItem(key, label, "no successful pull", "missing")
    when = run.finished_at or run.started_at
    extra = " (with errors)" if run.status == "completed_with_errors" else ""
    return StripItem(key, label, f"{_ago(when, now)}{extra}", "ok" if run.status == "succeeded" else "warn")


def _heartbeat(session: Session, role: str, label: str, now: datetime) -> StripItem:
    beat = session.scalar(
        select(ProcessHeartbeat).where(ProcessHeartbeat.role == role).order_by(ProcessHeartbeat.beat_at.desc()).limit(1)
    )
    if beat is None:
        return StripItem(role, label, "no heartbeat", "missing")
    age = now - _aware(beat.beat_at)
    if age > STALE_PROCESS:
        return StripItem(role, label, f"stale ({_ago(beat.beat_at, now)})", "danger")
    return StripItem(role, label, _ago(beat.beat_at, now), "ok")


def _task_deadline(task: Task) -> datetime | None:
    if task.lease_expires_at is not None:
        return _aware(task.lease_expires_at)
    started = task.updated_at or task.created_at
    return _aware(started) + TASK_MAX_RUNTIME if started is not None else None


def _jobs(session: Session, now: datetime) -> list[StripItem]:
    items: list[StripItem] = []
    tasks = session.scalars(select(Task).where(Task.status.in_(ACTIVE_TASKS)).order_by(Task.id.desc()).limit(20)).all()
    for task in tasks:
        deadline = _task_deadline(task)
        stuck = deadline is not None and now > deadline
        label = f"Task #{task.id} {task.task_type}"
        if stuck and deadline is not None:
            items.append(StripItem(f"task-{task.id}", label, f"STUCK (past {deadline.strftime('%H:%M')} UTC)", "danger", True))
        else:
            items.append(StripItem(f"task-{task.id}", label, task.status.replace("_", " "), "warn"))
    chains = session.scalars(
        select(SchedulerJobRun).where(SchedulerJobRun.status == "running").order_by(SchedulerJobRun.started_at.desc()).limit(10)
    ).all()
    for run in chains:
        deadline = _aware(run.started_at) + CHAIN_MAX_RUNTIME
        stuck = now > deadline
        label = f"Chain {run.chain_name}"
        if stuck:
            items.append(StripItem(f"chain-{run.id}", label, f"STUCK (running since {run.started_at.strftime('%H:%M')} UTC)", "danger", True))
        else:
            items.append(StripItem(f"chain-{run.id}", label, "running", "warn"))
    return items


def status_strip(session: Session, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    sources = [
        _source_item(session, "sam", "SAM", now),
        _source_item(session, "dibbs", "DIBBS", now),
        _source_item(session, "usaspending", "USAspending", now),
    ]
    jobs = _jobs(session, now)
    heartbeats = [
        _heartbeat(session, "worker", "Worker", now),
        _heartbeat(session, "scheduler", "Scheduler", now),
    ]
    return {
        "sources": [item.as_dict() for item in sources],
        "jobs": [item.as_dict() for item in jobs],
        "heartbeats": [item.as_dict() for item in heartbeats],
        "has_stuck": any(item.stuck for item in jobs),
    }
