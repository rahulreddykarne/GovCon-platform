"""Persist bot runs. The idempotency key is one bot plus one input version."""

from __future__ import annotations

import os
import socket
import sys
from collections.abc import Callable
from contextvars import ContextVar, Token
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from govcon.models import BotApproval, BotRun, ProcessHeartbeat
from govcon.ops.health import STALE_PROCESS

_FINISHED_OK = frozenset({"succeeded", "skipped", "waiting_approval"})
_STALE_RUNNING = timedelta(minutes=15)
_OWNER: ContextVar[str | None] = ContextVar("govcon_bot_worker", default=None)
INTERRUPTED_REASON = (
    "Worker stopped while this run was still marked running. It can be run again."
)


def set_bot_worker(worker_id: str | None) -> Token[str | None]:
    """Bind the worker id stamped on bot runs started on this thread."""
    return _OWNER.set(worker_id)


def reset_bot_worker(token: Token[str | None]) -> None:
    _OWNER.reset(token)


def _stamp_owner(run: BotRun) -> None:
    owner = _OWNER.get()
    if not owner:
        return
    current = dict(run.outputs or {})
    current["worker_id"] = owner
    run.outputs = current


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
        _stamp_owner(row)
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
    _stamp_owner(existing)
    return existing, True


def owner_still_running(worker_id: str | None, heartbeats: dict[str, datetime]) -> bool:
    """True when that worker process is still alive and its heartbeat is fresh."""
    if not worker_id:
        return False
    parts = worker_id.split(":")
    if len(parts) < 3:
        return False
    host, pid_text = parts[0], parts[1]
    beat = heartbeats.get(worker_id)
    fresh = beat is not None and datetime.now(UTC) - _aware(beat) <= STALE_PROCESS
    try:
        pid = int(pid_text)
    except ValueError:
        return False
    if not fresh or pid <= 0:
        return False
    if host != socket.gethostname():
        return fresh
    if sys.platform == "win32":
        return _windows_process_running(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # An inaccessible process may still own the fresh heartbeat.
        return True
    except OSError:
        return False
    return True


def _windows_process_running(pid: int) -> bool:
    """Query process status without sending Windows' signal-zero Ctrl+C event."""
    if sys.platform != "win32":
        raise RuntimeError("Windows process lookup called on a non-Windows platform")
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only
    if not handle:
        # Access denied is not evidence that a process with a fresh beat died.
        return ctypes.get_last_error() == 5
    try:
        return kernel.WaitForSingleObject(handle, 0) == 0x00000102  # WAIT_TIMEOUT
    finally:
        kernel.CloseHandle(handle)


def fail_interrupted_runs(
    session: Session,
    *,
    owner_alive: Callable[[str | None], bool] | None = None,
) -> int:
    """Mark running bot rows failed when their worker is gone. A failed row can be started again."""
    heartbeats = {
        row.instance_id: row.beat_at
        for row in session.scalars(select(ProcessHeartbeat).where(ProcessHeartbeat.role == "worker")).all()
    }
    check = owner_alive or (lambda worker_id: owner_still_running(worker_id, heartbeats))
    now = datetime.now(UTC)
    changed = 0
    rows = session.scalars(select(BotRun).where(BotRun.status == "running")).all()
    for row in rows:
        owner = (row.outputs or {}).get("worker_id")
        if check(owner if isinstance(owner, str) else None):
            continue
        outputs = dict(row.outputs or {})
        outputs["state"] = "incomplete"
        outputs["retryable"] = True
        row.status = "failed"
        row.finished_at = now
        row.error = INTERRUPTED_REASON
        row.outputs = outputs
        changed += 1
    return changed


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
