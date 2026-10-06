"""Persist bot runs. The idempotency key is one bot plus one input version."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from govcon.models import BotApproval, BotRun

_FINISHED_OK = frozenset({"succeeded", "skipped", "waiting_approval", "completed_with_errors"})
_STALE_RUNNING = timedelta(minutes=15)


def begin_run(
    session: Session,
    *,
    bot_name: str,
    idempotency_key: str,
    trigger: str,
    inputs: dict[str, Any],
    opportunity_id: int | None = None,
    source_revision: str | None = None,
    parent_run_id: int | None = None,
    force: bool = False,
) -> tuple[BotRun, bool]:
    """Return ``(run, started)``. ``started`` is false when this key already finished."""
    existing = session.scalar(select(BotRun).where(BotRun.idempotency_key == idempotency_key))
    if existing is None:
        row = BotRun(
            bot_name=bot_name,
            status="running",
            trigger=trigger,
            idempotency_key=idempotency_key,
            opportunity_id=opportunity_id,
            source_revision=source_revision,
            parent_run_id=parent_run_id,
            started_at=datetime.now(UTC),
            attempt=1,
            inputs=inputs,
        )
        try:
            with session.begin_nested():
                session.add(row)
                session.flush()
        except IntegrityError:
            existing = session.scalar(select(BotRun).where(BotRun.idempotency_key == idempotency_key))
            if existing is None:
                raise
            return _maybe_restart(existing, force=force, inputs=inputs)
        return row, True
    return _maybe_restart(existing, force=force, inputs=inputs)


def _maybe_restart(existing: BotRun, *, force: bool, inputs: dict[str, Any]) -> tuple[BotRun, bool]:
    now = datetime.now(UTC)
    stale = (
        existing.status == "running"
        and _aware(existing.started_at) < now - _STALE_RUNNING
    )
    if not force and existing.status in _FINISHED_OK:
        return existing, False
    if not force and existing.status in {"queued", "running"} and not stale:
        return existing, False
    existing.status = "running"
    existing.attempt = int(existing.attempt or 1) + 1
    existing.started_at = now
    existing.finished_at = None
    existing.error = None
    existing.outputs = None
    existing.inputs = inputs
    return existing, True


def finish_run(
    run: BotRun,
    status: str,
    *,
    outputs: dict[str, Any] | None = None,
    error: str | None = None,
) -> BotRun:
    run.status = status
    run.finished_at = datetime.now(UTC)
    run.outputs = outputs or {}
    run.error = error
    return run


def open_approval(
    session: Session,
    run: BotRun,
    *,
    kind: str,
    summary: str,
    evidence: dict[str, Any] | None = None,
    opportunity_id: int | None = None,
) -> BotApproval:
    existing = session.scalar(
        select(BotApproval).where(
            BotApproval.bot_run_id == run.id,
            BotApproval.kind == kind,
            BotApproval.status == "pending",
        )
    )
    if existing is not None:
        return existing
    row = BotApproval(
        bot_run_id=run.id,
        opportunity_id=opportunity_id if opportunity_id is not None else run.opportunity_id,
        kind=kind,
        status="pending",
        summary=summary,
        evidence=evidence or {},
    )
    session.add(row)
    session.flush()
    return row


def decide_approval(
    session: Session,
    approval_id: int,
    *,
    user_id: int,
    status: str,
    note: str | None,
) -> BotApproval | None:
    """Record a human decision. Does not send mail or change AI sharing."""
    if status not in {"approved", "rejected"}:
        raise ValueError("approval status must be approved or rejected")
    row = session.get(BotApproval, approval_id)
    if row is None or row.status != "pending":
        return row
    row.status = status
    row.decided_by_user_id = user_id
    row.decided_at = datetime.now(UTC)
    row.decision_note = (note or "").strip() or None
    return row


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value
