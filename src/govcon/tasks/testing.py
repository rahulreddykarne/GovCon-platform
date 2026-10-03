"""Run queued tasks in-process (tests, CLI ``--wait``, single-process setups)."""

from __future__ import annotations

from govcon.config import Settings, get_settings
from govcon.tasks.worker import run_once


def drain(
    settings: Settings | None = None,
    *,
    task_types: list[str] | None = None,
    opportunity_id: int | None = None,
    max_tasks: int = 50,
) -> list[tuple[int, str]]:
    """Run due tasks until none are left; returns ``(task_id, status)`` per run.

    ``opportunity_id`` limits the run to one opportunity's tasks, so tests on
    a shared database never run each other's work.
    """
    settings = settings or get_settings()
    results: list[tuple[int, str]] = []
    for _ in range(max_tasks):
        ran = run_once(settings, task_types=task_types, opportunity_id=opportunity_id)
        if ran is None:
            break
        results.append(ran)
    return results
