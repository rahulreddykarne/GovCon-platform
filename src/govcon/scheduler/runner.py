"""Phase 17 — APScheduler daemon.

Calling ``start_blocking_scheduler(settings)`` starts a blocking APScheduler
with all six job chains wired to their cron triggers.  Job failures are
caught, logged, and recorded to the DB; the scheduler continues running.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def start_blocking_scheduler(settings) -> None:
    """Start the APScheduler daemon and block until interrupted.

    All chain failures are logged and persisted; the scheduler itself never
    silently loses a job result.
    """
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    from govcon.scheduler.chains import run_chain

    scheduler = BlockingScheduler(timezone="UTC")

    def _make_job(chain_name: str):
        def _job():
            logger.info("scheduler: starting chain=%s", chain_name)
            try:
                result = run_chain(chain_name, settings, trigger="scheduler")
                if result.failed:
                    logger.error(
                        "scheduler: chain=%s FAILED at step=%s: %s",
                        chain_name,
                        result.failed_step,
                        result.error,
                    )
                else:
                    logger.info(
                        "scheduler: chain=%s succeeded steps=%s",
                        chain_name,
                        result.steps_completed,
                    )
            except Exception:
                logger.exception("scheduler: chain=%s uncaught error", chain_name)

        _job.__name__ = f"chain_{chain_name}"
        return _job

    # 06:30 daily — morning ingest chain
    scheduler.add_job(
        _make_job("morning_ingest"),
        CronTrigger(hour=6, minute=30, timezone="UTC"),
        id="morning_ingest",
        name="Morning ingest: SAM → DIBBS → match → alerts",
        misfire_grace_time=600,
        coalesce=True,
    )

    # 07:30 daily — USAspending delta
    scheduler.add_job(
        _make_job("usaspending"),
        CronTrigger(hour=7, minute=30, timezone="UTC"),
        id="usaspending",
        name="USAspending delta",
        misfire_grace_time=600,
        coalesce=True,
    )

    # 08:00 daily — embeddings + semantic
    scheduler.add_job(
        _make_job("embeddings"),
        CronTrigger(hour=8, minute=0, timezone="UTC"),
        id="embeddings",
        name="Embeddings → semantic matching",
        misfire_grace_time=600,
        coalesce=True,
    )

    # 12:00 daily — midday lightweight check
    scheduler.add_job(
        _make_job("midday_check"),
        CronTrigger(hour=12, minute=0, timezone="UTC"),
        id="midday_check",
        name="Midday deadline/amendment check",
        misfire_grace_time=600,
        coalesce=True,
    )

    # 18:00 daily — evening ingest chain
    scheduler.add_job(
        _make_job("evening_ingest"),
        CronTrigger(hour=18, minute=0, timezone="UTC"),
        id="evening_ingest",
        name="Evening ingest: SAM → DIBBS → match → alerts",
        misfire_grace_time=600,
        coalesce=True,
    )

    # 09:00 every Sunday — weekly sweep
    scheduler.add_job(
        _make_job("sunday_sweep"),
        CronTrigger(day_of_week="sun", hour=9, minute=0, timezone="UTC"),
        id="sunday_sweep",
        name="Sunday sweep: archive → cache → analytics → VACUUM",
        misfire_grace_time=1800,
        coalesce=True,
    )

    logger.info("scheduler: starting with 6 job chains")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("scheduler: shutting down")
    finally:
        scheduler.shutdown(wait=False)
