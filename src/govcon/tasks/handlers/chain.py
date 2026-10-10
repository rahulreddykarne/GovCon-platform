"""``scheduler_chain``: run a scheduler chain as a resumable task (ADR-063)."""

from __future__ import annotations

from govcon.scheduler.chain_tasks import CHAIN_TASK, chain_task_failed, run_chain_task
from govcon.tasks.registry import TaskHandler, register

register(TaskHandler(task_type=CHAIN_TASK, run=run_chain_task, on_failed=chain_task_failed))
