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
    """Importable callback for APScheduler's persistent job store."""
    from govcon.config import get_settings
    from govcon.scheduler.chains import run_chain
    result = run_chain(chain_name, _scheduler_settings or get_settings(), trigger="scheduler")
    if result.failed:
        logger.error("scheduler: chain=%s failed: %s", chain_name, result.error)


def start_blocking_scheduler(settings) -> None:
    """Only one daemon owns the persistent schedule at a time."""
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
            try:
                _start_scheduler(settings, leader_connection=connection)
            finally:
                connection.execute(text("SELECT pg_advisory_unlock(742901, 2)"))
                connection.commit()
    finally:
        engine.dispose()


def _configured_scheduler(settings):
    """Build persistent schedules while preserving saved due times."""
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

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
    from sqlalchemy import text
    from apscheduler.schedulers.base import SchedulerNotRunningError
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
