"""Phase 17 — APScheduler daemon.

Calling ``start_blocking_scheduler(settings)`` starts a blocking APScheduler
with all six job chains wired to their cron triggers.  Job failures are
caught, logged, and recorded to the DB; the scheduler continues running.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)
_scheduler_settings = None


def execute_scheduled_chain(chain_name: str) -> None:
    """Importable callback for APScheduler's persistent job store.

    Queues a durable ``scheduler_chain`` task (ADR-063); a worker runs it and
    resumes it at the next unfinished step if the worker dies.
    """
    from datetime import datetime, timezone

    from govcon.config import get_settings
    from govcon.db import session_scope
    from govcon.scheduler.chain_tasks import queue_chain

    settings = _scheduler_settings or get_settings()
    slot = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
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

    from govcon.db import make_engine
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


def _configured_scheduler(settings):
    """Build persistent schedules while preserving saved due times."""
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.util import undefined
    store = SQLAlchemyJobStore(url=settings.require_database_url())
    scheduler = BlockingScheduler(timezone="UTC", jobstores={"default": store}, job_defaults={"max_instances": 1})
    # Load persisted due times before replacing definitions so restart retains misfires.
    store.start(scheduler, "default")
    due_times = {job.id: job.next_run_time for job in store.get_all_jobs()}

    # 06:30 daily — morning ingest chain
    scheduler.add_job(
        execute_scheduled_chain,
        CronTrigger(hour=6, minute=30, timezone="UTC"),
        id="morning_ingest",
        replace_existing=True,
        next_run_time=due_times.get("morning_ingest", undefined),
        args=["morning_ingest"],
        name="Morning ingest: SAM → DIBBS → source changes → match → alerts",
        misfire_grace_time=600,
        coalesce=True,
    )

    # 07:30 daily — USAspending delta
    scheduler.add_job(
        execute_scheduled_chain,
        CronTrigger(hour=7, minute=30, timezone="UTC"),
        id="usaspending",
        replace_existing=True,
        next_run_time=due_times.get("usaspending", undefined),
        args=["usaspending"],
        name="USAspending delta",
        misfire_grace_time=600,
        coalesce=True,
    )

    # 08:00 daily — embeddings + semantic
    scheduler.add_job(
        execute_scheduled_chain,
        CronTrigger(hour=8, minute=0, timezone="UTC"),
        id="embeddings",
        replace_existing=True,
        next_run_time=due_times.get("embeddings", undefined),
        args=["embeddings"],
        name="Embeddings → semantic matching",
        misfire_grace_time=600,
        coalesce=True,
    )

    # 12:00 daily — midday lightweight check
    scheduler.add_job(
        execute_scheduled_chain,
        CronTrigger(hour=12, minute=0, timezone="UTC"),
        id="midday_check",
        replace_existing=True,
        next_run_time=due_times.get("midday_check", undefined),
        args=["midday_check"],
        name="Midday deadline/amendment check",
        misfire_grace_time=600,
        coalesce=True,
    )

    # 18:00 daily — evening ingest chain
    scheduler.add_job(
        execute_scheduled_chain,
        CronTrigger(hour=18, minute=0, timezone="UTC"),
        id="evening_ingest",
        replace_existing=True,
        next_run_time=due_times.get("evening_ingest", undefined),
        args=["evening_ingest"],
        name="Evening ingest: SAM → DIBBS → source changes → match → alerts",
        misfire_grace_time=600,
        coalesce=True,
    )

    # 09:00 every Sunday — weekly sweep
    scheduler.add_job(
        execute_scheduled_chain,
        CronTrigger(day_of_week="sun", hour=9, minute=0, timezone="UTC"),
        id="sunday_sweep",
        replace_existing=True,
        next_run_time=due_times.get("sunday_sweep", undefined),
        args=["sunday_sweep"],
        name="Sunday sweep: archive → cache → analytics → VACUUM",
        misfire_grace_time=1800,
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
