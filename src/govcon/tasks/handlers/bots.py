"""``bot_run``: the worker entry for the orchestrator."""

from __future__ import annotations

from govcon.db import session_scope
from govcon.models import Task
from govcon.tasks import queue
from govcon.tasks.registry import TaskHandler, register


def run_bot_task(settings, claim, heartbeat) -> str:
    del heartbeat
    with session_scope(settings) as db:
        task = db.get(Task, claim.task_id)
        if task is None:
            raise RuntimeError(f"task {claim.task_id} disappeared")
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
        )
        result = {"bot_run_id": run.id, "status": run.status, "state": (run.outputs or {}).get("state")}
    with session_scope(settings) as db:
        task = queue.guard_publish(db, claim)
        queue.complete(task, result)
    return "succeeded"


register(TaskHandler(task_type="bot_run", run=run_bot_task))
