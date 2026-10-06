"""Task handlers: ordered steps, each split into prepare / execute / publish.

A step follows the roadmap's pattern (§4.4):

1. ``prepare(session, task, ctx)`` runs in a short transaction that already
   holds the opportunity and task locks; it loads and validates inputs.
2. ``execute(inputs, ctx)`` runs with **no** database transaction open; this is
   where downloads, OCR and AI calls happen.
3. ``publish(session, task, output, ctx)`` runs in a new transaction after the
   lease is re-verified under the opportunity lock. It must recheck that the
   inputs are still current, then write results. The worker records the
   step's checkpoint (and completes the task after the last step) in the same
   commit.

Prepare and execute are re-run after a crash, so they must be free of side
effects other than budget accounting.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from govcon.config import Settings
from govcon.models import Task
from govcon.tasks.errors import TaskCancelled


def required_opportunity_id(value: int | None) -> int:
    """An opportunity-scoped handler cannot process a removed target."""
    if value is None:
        raise TaskCancelled("the task's opportunity was removed or is missing")
    return value


@dataclass
class StepContext:
    settings: Settings
    task_id: int
    opportunity_id: int | None
    payload: dict[str, Any]
    input_revision: dict[str, Any]
    # Set by publish of the final step; stored as the task's result.
    result: dict[str, Any] | None = None
    # Data a publish wants recorded with its checkpoint.
    checkpoint_data: dict[str, Any] | None = None


Prepare = Callable[[Session, Task, StepContext], Any]
Execute = Callable[[Any, StepContext], Any]
Publish = Callable[[Session, Task, Any, StepContext], None]


@dataclass
class Step:
    name: str
    prepare: Prepare
    publish: Publish
    execute: Execute | None = None
    timeout_seconds: int = 900


@dataclass
class TaskHandler:
    """Either ordered ``steps``, or ``run`` for work whose steps commit their own results.

    ``run(settings, claim, heartbeat) -> status`` must checkpoint each step in
    the same commit as the step's work and publish only under
    ``queue.guard_publish`` (scheduler chains use this).
    """

    task_type: str
    steps: list[Step] = field(default_factory=list)
    run: Callable[..., str] | None = None
    # Called in the same transaction when the task fails terminally.
    on_failed: Callable[[Session, Task], None] | None = None


_HANDLERS: dict[str, TaskHandler] = {}


def register(handler: TaskHandler) -> TaskHandler:
    _HANDLERS[handler.task_type] = handler
    return handler


def get_handler(task_type: str) -> TaskHandler:
    _load_builtin_handlers()
    try:
        return _HANDLERS[task_type]
    except KeyError:
        raise LookupError(f"no handler registered for task type {task_type!r}") from None


def registered_types() -> list[str]:
    _load_builtin_handlers()
    return sorted(_HANDLERS)


def _load_builtin_handlers() -> None:
    # Imported lazily: handlers depend on the service layer, which imports models.
    import govcon.tasks.handlers  # noqa: F401
