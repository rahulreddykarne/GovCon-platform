"""Phase 17 — APScheduler daemon.

Calling ``start_blocking_scheduler(settings)`` starts a blocking APScheduler
with all six job chains wired to their cron triggers.  Job failures are
caught, logged, and recorded to the DB; the scheduler continues running.
"""

from __future__ import annotations

import logging
from datetime import UTC

logger = logging.getLogger(__name__)
_scheduler_settings = None


def execute_scheduled_chain(chain_name: str, schedule_version: str | None = None) -> None:
    """Importable callback for APScheduler's persistent job store.

    Queues a durable ``scheduler_chain`` task (ADR-063); a worker runs it and
    resumes it at the next unfinished step if the worker dies.
    """
    from datetime import datetime

    from govcon.config import get_settings
    from govcon.db import session_scope
    from govcon.scheduler.chain_tasks import queue_chain

    del schedule_version  # stored on the job so a UTC schedule is not reused
    settings = _scheduler_settings or get_settings()
    slot = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M")
    with session_scope(settings) as db:
        task, created = queue_chain(db, chain_name, trigger="scheduler", slot=slot)
        task_id = task.id
    logger.info("scheduler: chain=%s queued as task %s%s", chain_name, task_id, "" if created else " (already queued)")


def start_blocking_scheduler(settings, *, embedded_worker: bool = True) -> None:
    """Only one daemon owns the persistent schedule at a time.

    With ``embedded_worker`` the daemon also runs a worker thread for chain
    tasks, so a single ``govcon scheduler start`` keeps working as before.
    """
    from sqlalchemy import text

    from govcon.db import make_engine, session_scope
    try:
        from govcon.prompting.registry import PromptSetupError, ensure_prompt_registry

        with session_scope(settings) as db:
            missing = ensure_prompt_registry(db, settings)
        if missing:
            logger.error("%s", PromptSetupError(missing))
    except Exception:
        logger.exception("prompt registry was not prepared")
    engine = make_engine(settings)
    try:
        with engine.connect() as connection:
            acquired = connection.scalar(text("SELECT pg_try_advisory_lock(742901, 2)"))
            connection.commit()
            if not acquired:
                logger.info("scheduler: another daemon owns the schedule")
                return
            worker_stop = None
            if embedded_worker:
                worker_stop = _start_embedded_worker(settings)
            try:
                _start_scheduler(settings, leader_connection=connection)
            finally:
                if worker_stop is not None:
                    worker_stop.set()
                connection.execute(text("SELECT pg_advisory_unlock(742901, 2)"))
                connection.commit()
    finally:
        engine.dispose()


def _start_embedded_worker(settings):
    """Run chain tasks in a daemon thread of the scheduler process."""
    from threading import Event, Thread

    from govcon.scheduler.chain_tasks import CHAIN_TASK
    from govcon.tasks.worker import run_worker

    stop = Event()
    Thread(
        target=run_worker, kwargs={"settings": settings, "task_types": [CHAIN_TASK], "stop_event": stop},
        name="scheduler-chain-worker", daemon=True,
    ).start()
    logger.info("scheduler: embedded worker started for chain tasks")
    return stop


def _operator_jobs(settings):
    """Owner clocks from Settings, or the code defaults when the row is absent."""
    from govcon.db import session_scope
    from govcon.scheduler.schedule import effective_jobs
    from govcon.workflow.app_settings import OPERATOR_SCHEDULE, get_setting

    saved: dict = {}
    try:
        with session_scope(settings) as db:
            value = get_setting(db, OPERATOR_SCHEDULE)
            raw = value.get("jobs") if isinstance(value, dict) else None
            if isinstance(raw, dict):
                saved = raw
    except Exception:
        logger.warning("scheduler: operator schedule could not be read; using the default clocks")
    return effective_jobs(saved)


def _configured_scheduler(settings):
    """Build persistent Pacific schedules while preserving due times from this version."""
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
    from apscheduler.schedulers.blocking import BlockingScheduler

    from govcon.scheduler.schedule import (
        OPERATOR_TZ_NAME,
        SCHEDULE_VERSION,
        cron_trigger,
        describe,
        preserved_next_run,
    )

    store = SQLAlchemyJobStore(url=settings.require_database_url())
    scheduler = BlockingScheduler(
        timezone=OPERATOR_TZ_NAME, jobstores={"default": store}, job_defaults={"max_instances": 1},
    )
    # Load persisted due times before replacing definitions so a restart can catch a missed run.
    store.start(scheduler, "default")
    existing = {job.id: job for job in store.get_all_jobs()}
    jobs = _operator_jobs(settings)
    titles = {
        "morning_ingest": "SAM, DIBBS, source documents, match, alerts",
        "usaspending": "USAspending delta",
        "embeddings": "embeddings after the morning ingest",
        "midday_check": "deadline and amendment check",
        "evening_ingest": "second SAM and DIBBS cycle",
        "sunday_sweep": "archive, cache, analytics, VACUUM",
    }
    for job_id, spec in jobs.items():
        previous = existing.get(job_id)
        scheduler.add_job(
            execute_scheduled_chain,
            cron_trigger(job_id, jobs),
            id=job_id,
            replace_existing=True,
            next_run_time=preserved_next_run(
                dict(previous.kwargs) if previous is not None else None,
                previous.next_run_time if previous is not None else None,
            ),
            args=[job_id],
            kwargs={"schedule_version": SCHEDULE_VERSION},
            name=f"{describe(job_id, jobs)}: {titles[job_id]}",
            misfire_grace_time=int(spec["grace"]),
            coalesce=True,
        )
    return scheduler


def _start_scheduler(settings, *, leader_connection) -> None:
    global _scheduler_settings
    _scheduler_settings = settings
    from threading import Event, Thread

    from apscheduler.schedulers.base import SchedulerNotRunningError
    from sqlalchemy import text
    scheduler = _configured_scheduler(settings)
    stopped = Event()

    def heartbeat():
        while not stopped.wait(15):
            try:
                leader_connection.execute(text("SELECT 1"))
                leader_connection.commit()
            except Exception:
                logger.exception("scheduler: leadership connection lost; stopping daemon")
                if scheduler.running:
                    scheduler.shutdown(wait=False)
                return
            try:
                from govcon.db import session_scope
                from govcon.ops.health import record_heartbeat

                with session_scope(settings) as db:
                    record_heartbeat(db, role="scheduler", instance_id="scheduler")
            except Exception:
                logger.warning("scheduler: heartbeat was not recorded")

    monitor = Thread(target=heartbeat, name="scheduler-leadership", daemon=True)
    monitor.start()
    logger.info("scheduler: starting with 6 persistent job chains")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("scheduler: shutting down")
    finally:
        stopped.set()
        monitor.join(timeout=5)
        try:
            scheduler.shutdown(wait=False)
        except SchedulerNotRunningError:
            pass
        _scheduler_settings = None
