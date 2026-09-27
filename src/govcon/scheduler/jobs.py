"""Individual scheduler job step functions.

Each function wraps an existing service call.  Steps record results to
``ingestion_runs`` using the same bookkeeping used by CLI ingest commands.
They return a ``StepResult`` so chain orchestrators can decide whether to
continue or abort the chain.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@dataclass
class StepResult:
    """Outcome of a single job step."""

    step: str
    status: str  # "succeeded" | "failed" | "skipped"
    inserted: int = 0
    updated: int = 0
    fetched: int = 0
    unchanged: int = 0
    error: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.status == "failed"

    def row_counts(self) -> dict:
        return {
            "fetched": self.fetched,
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
        }


# ---------------------------------------------------------------------------
# Ingest steps
# ---------------------------------------------------------------------------


def step_sam_ingest(session: Session, settings) -> StepResult:
    """Pull SAM.gov opportunities for the last 3 UTC days."""
    from govcon.ingest.runs import IngestStats, finish_run, start_run
    from govcon.ingest.sam_opportunities import SamApiError, assert_search_window, pull_sam_opportunities
    from govcon.logging import redact

    run = start_run(session, "sched:sam_ingest")
    try:
        api_key = settings.sam_api_key
        if not api_key:
            error_msg = "SAM_API_KEY not configured; skipping SAM ingest"
            logger.warning(error_msg)
            from govcon.ingest.runs import IngestStats
            finish_run(run, IngestStats(), status="skipped")
            return StepResult(step="sam_ingest", status="skipped", error=error_msg)

        from datetime import date, timedelta
        today = date.today()
        window_from = today - timedelta(days=3)
        assert_search_window(window_from, today)
        stats = pull_sam_opportunities(
            session,
            api_key=api_key,
            posted_from=window_from,
            posted_to=today,
            limit=1000,
            settings=settings,
        )
        status = "succeeded" if not stats.errors else "failed"
        finish_run(run, stats, status=status)
        return StepResult(
            step="sam_ingest",
            status=status,
            inserted=stats.inserted,
            updated=stats.updated,
            fetched=stats.fetched,
            unchanged=stats.unchanged,
            error="; ".join(stats.errors) if stats.errors else None,
        )
    except (SamApiError, ValueError, Exception) as exc:
        from govcon.ingest.runs import IngestStats
        err = redact(str(exc))
        finish_run(run, IngestStats(errors=[err]), status="failed")
        logger.error("sam_ingest failed: %s", err)
        return StepResult(step="sam_ingest", status="failed", error=err)


def step_dibbs_ingest(session: Session, settings) -> StepResult:
    """Pull the newest DIBBS daily index."""
    from govcon.ingest.dibbs import DibbsError, pull_dibbs_index
    from govcon.ingest.runs import IngestStats, finish_run, start_run
    from govcon.logging import redact

    run = start_run(session, "sched:dibbs_ingest")
    try:
        result = pull_dibbs_index(session, settings=settings)
        stats = result.stats
        status = "succeeded" if not stats.errors else "failed"
        finish_run(run, stats, status=status)
        return StepResult(
            step="dibbs_ingest",
            status=status,
            inserted=stats.inserted,
            updated=stats.updated,
            fetched=stats.fetched,
            unchanged=stats.unchanged,
            error="; ".join(stats.errors) if stats.errors else None,
        )
    except (DibbsError, OSError, ValueError, Exception) as exc:
        from govcon.ingest.runs import IngestStats
        err = redact(str(exc))
        finish_run(run, IngestStats(errors=[err]), status="failed")
        logger.error("dibbs_ingest failed: %s", err)
        return StepResult(step="dibbs_ingest", status="failed", error=err)


def step_match(session: Session) -> StepResult:
    """Run watchlist matching across all enabled watchlists."""
    from govcon.matching.engine import run_matching

    try:
        stats = run_matching(session)
        return StepResult(
            step="match",
            status="succeeded",
            inserted=getattr(stats, "inserted", 0),
            updated=getattr(stats, "updated", 0),
            fetched=getattr(stats, "evaluated", 0),
            unchanged=getattr(stats, "unchanged", 0),
            extra={"matched": getattr(stats, "matched", 0)},
        )
    except Exception as exc:
        logger.error("match failed: %s", exc)
        return StepResult(step="match", status="failed", error=str(exc))


def step_alerts(session: Session, settings) -> StepResult:
    """Send alert digests for new matches."""
    from govcon.alerts.digest import DigestDeliveryError, run_digest

    try:
        result = run_digest(session, settings=settings)
        return StepResult(
            step="alerts",
            status="succeeded",
            extra={
                "sent": result.sent,
                "new_count": result.new_count,
                "amendment_count": result.amendment_count,
                "channel": result.channel,
            },
        )
    except DigestDeliveryError as exc:
        logger.error("alerts failed: %s", exc)
        return StepResult(step="alerts", status="failed", error=str(exc))
    except Exception as exc:
        logger.error("alerts unexpected error: %s", exc)
        return StepResult(step="alerts", status="failed", error=str(exc))


def step_usaspending(session: Session, settings) -> StepResult:
    """Pull USAspending delta for enabled watchlist PSC/NAICS codes."""
    from govcon.ingest.runs import IngestStats, finish_run, start_run
    from govcon.ingest.usaspending import ingest_usaspending_awards
    from govcon.logging import redact

    run = start_run(session, "sched:usaspending")
    try:
        totals = ingest_usaspending_awards(session, settings=settings)
        status = "succeeded" if not totals.errors else "failed"
        finish_run(run, totals, status=status)
        return StepResult(
            step="usaspending",
            status=status,
            inserted=totals.inserted,
            updated=totals.updated,
            fetched=totals.fetched,
            unchanged=totals.unchanged,
            error="; ".join(totals.errors) if totals.errors else None,
        )
    except Exception as exc:
        from govcon.ingest.runs import IngestStats
        err = redact(str(exc))
        finish_run(run, IngestStats(errors=[err]), status="failed")
        logger.error("usaspending failed: %s", err)
        return StepResult(step="usaspending", status="failed", error=err)


def step_embeddings(session: Session, settings) -> StepResult:
    """Generate embeddings for opportunities that lack them."""
    try:
        from govcon.enrich.embeddings import build_watchlist_profiles, get_default_provider, run_embedding_job

        provider = get_default_provider(settings.embedding_model)
        stats = run_embedding_job(session, provider, only_missing=True)
        wl_stats = build_watchlist_profiles(session, provider)
        return StepResult(
            step="embeddings",
            status="succeeded",
            inserted=stats.get("embedded", 0),
            unchanged=stats.get("skipped", 0),
            extra={"watchlists_updated": wl_stats.get("updated", 0)},
        )
    except Exception as exc:
        logger.error("embeddings failed: %s", exc)
        return StepResult(step="embeddings", status="failed", error=str(exc))


def step_semantic_match(session: Session, settings) -> StepResult:
    """Compute semantic recommendations for all enabled watchlists."""
    try:
        from govcon.enrich.embeddings import get_default_provider
        from govcon.matching.semantic import all_recommendations
        from govcon.matching.watchlists import list_watchlists

        provider = get_default_provider(settings.embedding_model)
        watchlists = list_watchlists(session, include_disabled=False)
        count = 0
        for wl in watchlists:
            all_recommendations(session, watchlist_id=wl.id, provider=provider, limit=20)
            count += 1
        return StepResult(
            step="semantic_match",
            status="succeeded",
            extra={"watchlists_processed": count},
        )
    except Exception as exc:
        logger.error("semantic_match failed: %s", exc)
        return StepResult(step="semantic_match", status="failed", error=str(exc))


def step_midday_deadline_check(session: Session, settings) -> StepResult:
    """Refresh local bid eligibility without using source API quota."""
    from govcon.ingest.runs import IngestStats, finish_run, start_run
    from govcon.ingest.status import refresh_opportunity_statuses

    run = start_run(session, "sched:midday_check")
    try:
        stats = refresh_opportunity_statuses(session)
        finish_run(run, stats, status="succeeded")
        return StepResult(
            step="midday_deadline_check",
            status="succeeded",
            updated=stats.updated,
            extra={"reclassified": stats.updated},
        )
    except Exception as exc:
        from govcon.ingest.runs import IngestStats
        err = str(exc)
        finish_run(run, IngestStats(errors=[err]), status="failed")
        logger.error("midday_deadline_check failed: %s", err)
        return StepResult(step="midday_deadline_check", status="failed", error=err)


def step_archive_sweep(session: Session) -> StepResult:
    """Reclassify SAM and DIBBS rows as dates and source facts change."""
    from govcon.ingest.runs import IngestStats, finish_run, start_run
    from govcon.ingest.status import refresh_opportunity_statuses

    run = start_run(session, "sched:archive_sweep")
    try:
        stats = refresh_opportunity_statuses(session)
        finish_run(run, stats, status="succeeded")
        return StepResult(
            step="archive_sweep",
            status="succeeded",
            updated=stats.updated,
            extra={"reclassified": stats.updated},
        )
    except Exception as exc:
        from govcon.ingest.runs import IngestStats
        err = str(exc)
        finish_run(run, IngestStats(errors=[err]), status="failed")
        logger.error("archive_sweep failed: %s", err)
        return StepResult(step="archive_sweep", status="failed", error=err)


def step_cache_refresh(session: Session, settings) -> StepResult:
    """Refresh watchlist profile embeddings (cache refresh)."""
    try:
        from govcon.enrich.embeddings import build_watchlist_profiles, get_default_provider

        provider = get_default_provider(settings.embedding_model)
        stats = build_watchlist_profiles(session, provider)
        return StepResult(
            step="cache_refresh",
            status="succeeded",
            extra={"watchlists_updated": stats.get("updated", 0)},
        )
    except Exception as exc:
        logger.error("cache_refresh failed: %s", exc)
        return StepResult(step="cache_refresh", status="failed", error=str(exc))


def step_analytics_refresh(session: Session) -> StepResult:
    """Exercise the same persisted-data analytics calculation used by the UI."""
    from govcon.learning.analytics import outcome_analytics
    try:
        report = outcome_analytics(session)
        return StepResult(step="analytics_refresh", status="succeeded", extra={"submitted": report.total_submitted, "no_bid": report.total_no_bid})
    except Exception as exc:
        logger.error("analytics_refresh failed: %s", exc)
        return StepResult(step="analytics_refresh", status="failed", error=str(exc))


def step_vacuum_analyze(session: Session, settings) -> StepResult:
    """Run VACUUM ANALYZE on the govcon database.

    VACUUM cannot run inside a transaction block.  A dedicated engine with
    AUTOCOMMIT isolation is used so this step is completely independent of the
    caller's session/transaction.
    """
    from sqlalchemy import text

    try:
        from govcon.db import make_engine

        engine = make_engine(settings)
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text("VACUUM ANALYZE"))
        engine.dispose()
        return StepResult(step="vacuum_analyze", status="succeeded")
    except Exception as exc:
        logger.error("vacuum_analyze failed: %s", exc)
        return StepResult(step="vacuum_analyze", status="failed", error=str(exc))
