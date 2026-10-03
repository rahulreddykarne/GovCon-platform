"""Phase 17 — Job chain orchestration.

Each chain is a sequence of steps where a hard predecessor failure aborts
all subsequent dependents in that chain.  Unrelated chains (independent
APScheduler jobs) are not affected.

A chain may mark steps *soft*: their failure is recorded but does not stop
later steps. Ingest steps are soft in the ingest chains, so a SAM outage or a
malformed DIBBS line still lets matching and alerts run on whatever was
ingested. A chain whose soft steps failed, or whose steps finished with
per-record errors, ends ``completed_with_errors``; only a hard failure ends
``failed``.

Every chain execution writes a ``SchedulerJobRun`` row so the result is
always visible in ``/ops``.  Each step also writes an ``ingestion_runs``
row via the existing bookkeeping layer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy.orm import Session

from govcon.scheduler.jobs import StepResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Chain definitions
# ---------------------------------------------------------------------------

@dataclass
class ChainDef:
    """Static definition of a scheduled job chain."""

    name: str
    description: str
    cron: str  # human-readable cron schedule description
    steps: list[str]
    # Steps whose failure is recorded but does not stop the rest of the chain.
    soft_steps: frozenset[str] = frozenset()


CHAIN_DEFINITIONS: dict[str, ChainDef] = {
    "morning_ingest": ChainDef(
        name="morning_ingest",
        description="SAM ingest → DIBBS ingest → source-change workflow → match → rank → auto-pursue → alerts",
        cron="06:30 daily",
        steps=["sam_ingest", "dibbs_ingest", "source_changes", "match", "rank", "auto_pursue", "alerts"],
        soft_steps=frozenset({"sam_ingest", "dibbs_ingest", "source_changes", "rank", "auto_pursue"}),
    ),
    "usaspending": ChainDef(
        name="usaspending",
        description="USAspending delta ingest → award-match outcome suggestions",
        cron="07:30 daily",
        steps=["usaspending", "outcome_suggestions"],
        soft_steps=frozenset({"outcome_suggestions"}),
    ),
    "embeddings": ChainDef(
        name="embeddings",
        description="Opportunity embeddings → semantic recommendations → rank",
        cron="08:00 daily",
        steps=["embeddings", "semantic_match", "rank"],
        soft_steps=frozenset({"rank"}),
    ),
    "midday_check": ChainDef(
        name="midday_check",
        description="Deadline/amendment check → review reminders and escalations → company SAM registration refresh",
        cron="12:00 daily",
        steps=["midday_deadline_check", "review_escalations", "company_registration"],
        soft_steps=frozenset({"review_escalations", "company_registration"}),
    ),
    "evening_ingest": ChainDef(
        name="evening_ingest",
        description="Second SAM/DIBBS → source-change workflow → match → rank → auto-pursue → alerts",
        cron="18:00 daily",
        steps=["sam_ingest", "dibbs_ingest", "source_changes", "match", "rank", "auto_pursue", "alerts"],
        soft_steps=frozenset({"sam_ingest", "dibbs_ingest", "source_changes", "rank", "auto_pursue"}),
    ),
    "sunday_sweep": ChainDef(
        name="sunday_sweep",
        description="Archive sweep → cache refresh → analytics refresh → VACUUM ANALYZE",
        cron="09:00 every Sunday",
        steps=["archive_sweep", "cache_refresh", "analytics_refresh", "vacuum_analyze"],
    ),
}

ALL_CHAIN_NAMES: list[str] = list(CHAIN_DEFINITIONS.keys())


def _chain_lock_key(chain_name: str) -> int:
    # Both ingest schedules operate on the same opportunity/feed state.
    group = "opportunity_ingest" if chain_name in {"morning_ingest", "evening_ingest"} else chain_name
    import hashlib
    return 100 + (int.from_bytes(hashlib.sha256(group.encode()).digest()[:4], "big") % (2**31 - 100))


# ---------------------------------------------------------------------------
# Chain runner
# ---------------------------------------------------------------------------


def run_chain(chain_name: str, settings, *, trigger: str = "manual") -> "ChainResult":
    """Serialize each chain across workers; crashes automatically release locks."""
    from sqlalchemy import text
    from govcon.db import make_engine, session_scope
    from govcon.models import SchedulerJobRun

    if chain_name not in CHAIN_DEFINITIONS:
        raise ValueError(f"Unknown chain: {chain_name!r}")
    engine = make_engine(settings)
    lock_key = _chain_lock_key(chain_name)
    try:
        with engine.connect() as lock_connection:
            acquired = lock_connection.scalar(text("SELECT pg_try_advisory_lock(742901, :key)"), {"key": lock_key})
            lock_connection.commit()
            if not acquired:
                return ChainResult(chain_name, None, "skipped", [], None, "another worker is executing a chain")
            try:
                # Only this chain/resource is locked; independent chains stay live.
                with session_scope(settings) as db:
                    from sqlalchemy import select
                    related = [name for name in CHAIN_DEFINITIONS if _chain_lock_key(name) == lock_key]
                    for row in db.scalars(select(SchedulerJobRun).where(SchedulerJobRun.status == "running", SchedulerJobRun.chain_name.in_(related))):
                        row.status = "failed"
                        row.finished_at = datetime.now(timezone.utc)
                        row.error = "worker stopped before completion; recovered after advisory lock release"
                return _run_chain_locked(chain_name, settings, trigger=trigger, lock_connection=lock_connection)
            finally:
                lock_connection.execute(text("SELECT pg_advisory_unlock(742901, :key)"), {"key": lock_key})
                lock_connection.commit()
    except Exception as exc:
        logger.exception("chain=%s: scheduler coordination failed", chain_name)
        return ChainResult(chain_name, None, "failed", [], None, str(exc))
    finally:
        engine.dispose()


def _run_chain_locked(chain_name: str, settings, *, trigger: str, lock_connection) -> "ChainResult":
    """Execute a named chain and persist the result.

    Each step runs in its own ``session_scope`` so that a step failure or a
    step that needs ``AUTOCOMMIT`` (e.g. VACUUM ANALYZE) cannot corrupt the
    chain-level session.  Returns a ``ChainResult`` describing the outcome.
    Never raises; all errors are captured and recorded.
    """
    from govcon.db import session_scope
    from govcon.models import SchedulerJobRun

    chain_def = CHAIN_DEFINITIONS.get(chain_name)
    if chain_def is None:
        raise ValueError(f"Unknown chain: {chain_name!r}. Available: {list(CHAIN_DEFINITIONS)}")

    started_at = datetime.now(timezone.utc)
    steps_completed: list[str] = []
    step_results: list[StepResult] = []
    failed_step: str | None = None
    chain_error: str | None = None
    run_id: int | None = None
    soft_errors: list[str] = []

    # Write the initial "running" record.
    try:
        with session_scope(settings) as db:
            job_run = SchedulerJobRun(
                chain_name=chain_name,
                trigger=trigger,
                started_at=started_at,
                status="running",
                steps_completed=[],
            )
            db.add(job_run)
            db.flush()
            run_id = job_run.id
    except Exception as exc:
        logger.exception("chain=%s: failed to write initial job_run row", chain_name)
        chain_error = str(exc)
        return ChainResult(
            chain_name=chain_name,
            run_id=None,
            status="failed",
            steps_completed=[],
            failed_step=None,
            error=chain_error,
        )

    # Execute each step in its own session.
    for step_name in chain_def.steps:
        # Stop before a new step if the connection that owns our lock was lost.
        from sqlalchemy import text
        try:
            lock_connection.execute(text("SELECT 1"))
            lock_connection.commit()
        except Exception as exc:
            failed_step = step_name
            chain_error = f"chain lock connection was lost: {exc}"
            break
        step_fn = _STEP_FUNCTIONS.get(step_name)
        if step_fn is None:
            logger.error("chain=%s step=%s: no implementation found", chain_name, step_name)
            failed_step = step_name
            chain_error = f"No implementation for step '{step_name}'"
            break

        logger.info("chain=%s step=%s starting", chain_name, step_name)
        try:
            with session_scope(settings) as step_db:
                result = _invoke_step(step_fn, step_db, settings)
        except Exception as exc:
            result = StepResult(step=step_name, status="failed", error=str(exc))
            logger.exception("chain=%s step=%s uncaught error", chain_name, step_name)

        step_results.append(result)

        if result.failed and step_name in chain_def.soft_steps:
            logger.error(
                "chain=%s step=%s FAILED (soft): %s — continuing with later steps",
                chain_name,
                step_name,
                result.error,
            )
            soft_errors.append(f"{step_name}: {result.error}")
            continue

        if result.failed:
            logger.error(
                "chain=%s step=%s FAILED: %s — aborting chain",
                chain_name,
                step_name,
                result.error,
            )
            failed_step = step_name
            chain_error = result.error
            break

        if result.status == "completed_with_errors":
            soft_errors.append(f"{step_name}: {result.error}")
        logger.info("chain=%s step=%s %s", chain_name, step_name, result.status)
        steps_completed.append(step_name)

    finished_at = datetime.now(timezone.utc)
    if failed_step is not None:
        status = "failed"
    elif soft_errors:
        status = "completed_with_errors"
        chain_error = "; ".join(soft_errors)
    else:
        status = "succeeded"
    row_counts = {r.step: r.row_counts() for r in step_results}

    # Update the job_run record in a fresh session.
    try:
        with session_scope(settings) as db:
            from sqlalchemy import select as sa_select
            job_run = db.scalars(
                sa_select(SchedulerJobRun).where(SchedulerJobRun.id == run_id)
            ).first()
            if job_run is not None:
                job_run.finished_at = finished_at
                job_run.status = status
                job_run.steps_completed = steps_completed
                job_run.failed_step = failed_step
                job_run.error = chain_error
                job_run.row_counts = row_counts
    except Exception as exc:
        logger.exception("chain=%s: failed to update job_run id=%s", chain_name, run_id)

    return ChainResult(
        chain_name=chain_name,
        run_id=run_id,
        status=status,
        steps_completed=steps_completed,
        failed_step=failed_step,
        error=chain_error,
        step_results=step_results,
    )


@dataclass
class ChainResult:
    """Outcome of a chain execution."""

    chain_name: str
    run_id: int | None
    status: str
    steps_completed: list[str]
    failed_step: str | None
    error: str | None
    step_results: list[StepResult] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.status == "failed"

    def summary_row_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.step_results:
            for k, v in r.row_counts().items():
                counts[f"{r.step}.{k}"] = v
        return counts


# ---------------------------------------------------------------------------
# Step dispatch table — maps step name → callable
# ---------------------------------------------------------------------------


def _invoke_step(fn, db: Session, settings):
    """Call a step function with the correct signature."""
    import inspect

    sig = inspect.signature(fn)
    params = list(sig.parameters.keys())
    if "settings" in params:
        return fn(db, settings)
    return fn(db)


def _build_step_table() -> dict[str, Callable]:
    from govcon.scheduler import jobs

    return {
        "sam_ingest": jobs.step_sam_ingest,
        "dibbs_ingest": jobs.step_dibbs_ingest,
        "source_changes": jobs.step_source_changes,
        "match": jobs.step_match,
        "alerts": jobs.step_alerts,
        "usaspending": jobs.step_usaspending,
        "embeddings": jobs.step_embeddings,
        "semantic_match": jobs.step_semantic_match,
        "rank": jobs.step_rank,
        "auto_pursue": jobs.step_auto_pursue,
        "review_escalations": jobs.step_review_escalations,
        "company_registration": jobs.step_company_registration,
        "outcome_suggestions": jobs.step_outcome_suggestions,
        "midday_deadline_check": jobs.step_midday_deadline_check,
        "archive_sweep": jobs.step_archive_sweep,
        "cache_refresh": jobs.step_cache_refresh,
        "analytics_refresh": jobs.step_analytics_refresh,
        "vacuum_analyze": jobs.step_vacuum_analyze,
    }


_STEP_FUNCTIONS: dict[str, Callable] = _build_step_table()
