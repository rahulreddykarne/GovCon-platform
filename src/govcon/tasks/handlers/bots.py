"""``bot_run``: the worker entry for the orchestrator."""

from __future__ import annotations

from govcon.db import session_scope
from govcon.tasks import queue
from govcon.tasks.registry import TaskHandler, register


def run_bot_task(settings, claim, heartbeat) -> str:
    heartbeat.set_deadline(4 * 3600)
    with session_scope(settings) as db:
        task = queue.guard_publish(db, claim)
        payload = dict(task.payload or {})
    bot_name = payload.get("bot_name")
    if bot_name != "orchestrator":
        raise RuntimeError(f"bot task {claim.task_id} has no orchestrator payload")
    from govcon.bots.orchestrator import execute_orchestrator

    with session_scope(settings) as db:
        run = execute_orchestrator(
            db, settings,
            trigger=str(payload.get("trigger") or "worker"),
            slot=str(payload["slot"]),
            pull=bool(payload.get("pull")),
            persist_start=True,
            task_claim=claim,
        )
        result = {"bot_run_id": run.id, "status": run.status, "state": (run.outputs or {}).get("state")}
        if heartbeat.lost:
            raise queue.LeaseLost(f"task {claim.task_id}: heartbeat lost")
        task = queue.guard_publish(db, claim)
        if run.status in {"running", "queued"}:
            raise RuntimeError(f"bot run {run.id} is still owned by another execution")
        if run.status in {"failed", "completed_with_errors"}:
            task.result = result
            queue.retry_later(db, task, RuntimeError(run.error or f"bot run {run.id} is incomplete"), settings=settings)
            return task.status
        queue.complete(task, result)
    return "succeeded"


register(TaskHandler(task_type="bot_run", run=run_bot_task))
