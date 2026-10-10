"""Hard time limits for scheduler chain steps.

A step runs on a helper thread with its own session. The chain waits at most
the step's limit; past it, the step's database backend is terminated (so its
open transaction rolls back and can never commit) and the step is recorded as
failed with the reason. Python cannot kill the thread itself, so a step blocked
on the network is abandoned: its next database call fails and it exits.
"""

from __future__ import annotations

import contextvars
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

from govcon.config import Settings
from govcon.scheduler.jobs import StepResult

logger = logging.getLogger(__name__)

_STEP_SETTING = {
    "sam_ingest": "sam_ingest_timeout_seconds",
    "dibbs_ingest": "dibbs_ingest_timeout_seconds",
    "usaspending": "usaspending_ingest_timeout_seconds",
    "source_documents": "source_documents_timeout_seconds",
    "embeddings": "maintenance_step_timeout_seconds",
    "semantic_match": "maintenance_step_timeout_seconds",
    "analytics_refresh": "maintenance_step_timeout_seconds",
    "vacuum_analyze": "maintenance_step_timeout_seconds",
}
# Slack on top of the summed step limits before a running chain counts as stuck.
CHAIN_SLACK_SECONDS = 600
# How long a step already committing its result may take before it is abandoned too.
PUBLISH_GRACE_SECONDS = 60


def step_timeout(step: str, settings: Settings) -> int:
    return int(getattr(settings, _STEP_SETTING.get(step, "scheduler_step_timeout_seconds")))


def chain_max_seconds(chain_name: str, settings: Settings) -> int:
    """The longest a chain can legitimately run when every step uses its full limit."""
    from govcon.scheduler.chains import CHAIN_DEFINITIONS

    chain = CHAIN_DEFINITIONS.get(chain_name)
    if chain is None:
        return settings.scheduler_step_timeout_seconds + CHAIN_SLACK_SECONDS
    return sum(step_timeout(step, settings) for step in chain.steps) + CHAIN_SLACK_SECONDS


class StepAbandoned(RuntimeError):
    """The chain stopped waiting for this step; its result must not be published."""


@dataclass
class _StepState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    pid: int | None = None
    abandoned: bool = False
    publishing: bool = False
    result: StepResult | None = None
    error: BaseException | None = None


def _terminate(settings: Settings, pid: int | None) -> None:
    if pid is None:
        return
    from govcon.db import session_scope

    try:
        with session_scope(settings) as db:
            db.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
    except Exception:
        logger.exception("could not stop database backend %s of a timed-out step", pid)


def _duration(seconds: int) -> str:
    return f"{seconds // 60} min" if seconds >= 120 else f"{seconds} s"


def run_step_bounded(
    step_name: str,
    step_fn: Callable[..., StepResult],
    settings: Settings,
    *,
    timeout: int | None = None,
    publish: Callable[[Session, StepResult], None] | None = None,
) -> StepResult:
    """Run one step in its own transaction; ``publish`` runs in that transaction before commit.

    Returns a failed ``StepResult`` with ``extra["timed_out"]`` when the limit passes.
    Exceptions raised by the step or by ``publish`` propagate to the caller.
    """
    from govcon.db import session_scope
    from govcon.scheduler.chains import _invoke_step

    limit = timeout if timeout is not None else step_timeout(step_name, settings)
    state = _StepState()

    def target() -> None:
        try:
            with session_scope(settings) as db:
                db.execute(text("SELECT set_config('lock_timeout', :value, true)"),
                           {"value": f"{settings.scheduler_lock_timeout_seconds}s"})
                state.pid = db.scalar(text("SELECT pg_backend_pid()"))
                result = _invoke_step(step_fn, db, settings)
                with state.lock:
                    if state.abandoned:
                        raise StepAbandoned(step_name)
                    state.publishing = True
                if publish is not None:
                    publish(db, result)
            state.result = result
        except BaseException as exc:  # noqa: BLE001  re-raised on the chain thread
            state.error = exc

    context = contextvars.copy_context()
    worker = threading.Thread(target=context.run, args=(target,), name=f"chain-step-{step_name}", daemon=True)
    worker.start()
    worker.join(limit)
    if worker.is_alive():
        with state.lock:
            if not state.publishing:
                state.abandoned = True
        if not state.abandoned:
            worker.join(PUBLISH_GRACE_SECONDS)
        if worker.is_alive():
            state.abandoned = True
            _terminate(settings, state.pid)
            message = (f"timed out after {_duration(limit)}; the step was stopped and its "
                       "uncommitted work rolled back")
            logger.error("step=%s %s", step_name, message)
            return StepResult(step=step_name, status="failed", error=message,
                              extra={"timed_out": True, "timeout_seconds": limit})
    if state.error is not None:
        raise state.error
    assert state.result is not None
    return state.result
