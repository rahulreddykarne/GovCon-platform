"""Individual scheduler job step functions.

Each function wraps an existing service call.  Steps record results to
``ingestion_runs`` using the same bookkeeping used by CLI ingest commands.
They return a ``StepResult`` so chain orchestrators can decide whether to
continue or abort the chain.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@dataclass
class StepResult:
    """Outcome of a single job step."""

    step: str
    # "succeeded" | "completed_with_errors" (per-record problems; not a failure)
    # | "failed" | "skipped"
    status: str
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
    from govcon.ingest.sam_opportunities import (
        SamApiError,
        assert_search_window,
        pull_sam_opportunities,
    )
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

        from datetime import UTC, datetime, timedelta
        today = datetime.now(UTC).date()
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
        # Skipped records are reported, not fatal: the rest were ingested.
        status = "succeeded" if not stats.errors else "completed_with_errors"
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
    except (SamApiError, ValueError, Exception) as exc:  # noqa: BLE001  boundary must record any failure
        from govcon.ingest.runs import IngestStats
        err = redact(str(exc))
        finish_run(run, IngestStats(errors=[err]), status="failed")
        logger.error("sam_ingest failed: %s", err)
        return StepResult(step="sam_ingest", status="failed", error=err)


def step_dibbs_ingest(session: Session, settings) -> StepResult:
    """Pull every DIBBS daily index listed since the last one ingested."""
    from govcon.ingest.dibbs import DibbsError, pull_dibbs_index
    from govcon.ingest.runs import IngestStats, finish_run, start_run
    from govcon.logging import redact

    run = start_run(session, "sched:dibbs_ingest")
    try:
        result = pull_dibbs_index(session, settings=settings)
        stats = result.stats
        # Malformed index lines are reported, not fatal: valid lines were ingested.
        status = "succeeded" if not stats.errors else "completed_with_errors"
        finish_run(run, stats, status=status, details=result.run_details())
        return StepResult(
            step="dibbs_ingest",
            status=status,
            inserted=stats.inserted,
            updated=stats.updated,
            fetched=stats.fetched,
            unchanged=stats.unchanged,
            error="; ".join(stats.errors) if stats.errors else None,
        )
    except (DibbsError, OSError, ValueError, Exception) as exc:  # noqa: BLE001  boundary must record any failure
        from govcon.ingest.runs import IngestStats
        err = redact(str(exc))
        finish_run(run, IngestStats(errors=[err]), status="failed")
        logger.error("dibbs_ingest failed: %s", err)
        return StepResult(step="dibbs_ingest", status="failed", error=err)


def step_source_documents(session: Session, settings) -> StepResult:
    """Drain source-document work in bounded batches with a durable scan cursor.

    Failed notices remain eligible on the next pass. The cursor advances past
    failures too, so one unavailable source cannot starve the rest of the backlog.
    """
    from sqlalchemy import func, select
    from sqlalchemy.dialects.postgresql import insert

    from govcon.enrich.attachment_refs import (
        DIBBS_RFQ_PDF,
        SAM_DESCRIPTION,
        attachment_refs_for,
    )
    from govcon.enrich.attachments import download_attachments
    from govcon.ingest.runs import IngestStats, finish_run, start_run
    from govcon.logging import redact
    from govcon.models import AppSetting, Opportunity, StoredFile

    run = start_run(session, "sched:source_documents")
    try:
        cursor_key = "source_document_scan"
        session.execute(insert(AppSetting).values(key=cursor_key, value={"after_id": 0})
                        .on_conflict_do_nothing(index_elements=[AppSetting.key]))
        cursor = session.scalar(select(AppSetting).where(AppSetting.key == cursor_key).with_for_update())
        assert cursor is not None
        upper = session.scalar(select(func.max(Opportunity.id)).where(Opportunity.source.in_(("sam", "dibbs")))) or 0
        after = int((cursor.value or {}).get("after_id") or 0)
        if after >= upper:
            after = 0
        batch = 40
        chosen: list[Opportunity] = []
        scanned = 0
        while after < upper and len(chosen) < batch:
            candidates = _source_document_page(session, after, upper)
            if not candidates:
                after = upper
                break
            retained = set(session.execute(select(StoredFile.opportunity_id, StoredFile.url).where(
                StoredFile.opportunity_id.in_([opp.id for opp in candidates]),
                StoredFile.sha256.is_not(None), StoredFile.active.is_(True),
                StoredFile.extraction_status != "download_failed",
            )).all())
            for opportunity in candidates:
                after = opportunity.id
                scanned += 1
                refs = [ref for ref in attachment_refs_for(opportunity)
                        if ref.source_metadata.get("origin") in {SAM_DESCRIPTION, DIBBS_RFQ_PDF}]
                if any((opportunity.id, ref.url) not in retained for ref in refs):
                    chosen.append(opportunity)
                if len(chosen) == batch:
                    break
        cursor.value = {"after_id": after if after < upper else 0}
        errors: list[str] = []
        fetched = 0
        for opportunity in chosen:
            try:
                files = download_attachments(session, opportunity, settings=settings)
                if files and any(file.extraction_status == "download_failed" for file in files):
                    errors.append(f"opportunity {opportunity.id}: source document download failed")
                    continue
                fetched += 1
            except Exception as exc:  # noqa: BLE001  one notice must not stop the batch
                errors.append(redact(f"opportunity {opportunity.id}: {exc}"))
        stats = IngestStats(fetched=fetched, errors=errors)
        details = {"candidates": len(chosen), "scanned": scanned, "backlog_scan_pending": after < upper}
        status = "completed_with_errors" if errors else "succeeded"
        finish_run(run, stats, status=status, details=details)
        return StepResult(
            step="source_documents",
            status=status,
            fetched=fetched,
            error="; ".join(errors) if errors else None,
            extra=details,
        )
    except Exception as exc:  # noqa: BLE001  boundary must record any failure
        err = redact(str(exc))
        finish_run(run, IngestStats(errors=[err]), status="failed")
        logger.error("source_documents failed: %s", err)
        return StepResult(step="source_documents", status="failed", error=err)


def _source_document_page(session: Session, after: int, upper: int):
    from sqlalchemy import select

    from govcon.models import Opportunity

    return session.scalars(select(Opportunity).where(
        Opportunity.source.in_(("sam", "dibbs")), Opportunity.id > after, Opportunity.id <= upper,
    ).order_by(Opportunity.id).limit(200)).all()


def step_source_changes(session: Session, settings) -> StepResult:
    """Handle material source changes found by ingest.

    For opportunities with a pursuit or review session: fetch new attachments,
    rerun compliance, reopen review, and invalidate proposal approval and
    submission readiness. Per-opportunity errors are recorded and do not stop
    matching and alerts.
    """
    from govcon.ingest.runs import IngestStats, finish_run, start_run
    from govcon.logging import redact
    from govcon.workflow.invalidation import process_pending_source_changes

    run = start_run(session, "sched:source_changes")
    try:
        summary = process_pending_source_changes(session, settings=settings)
        errors = [redact(e) for e in summary["errors"]]
        stats = IngestStats(fetched=summary["events"], updated=summary["invalidated"], errors=errors)
        finish_run(run, stats, status="completed_with_errors" if errors else "succeeded", details=summary)
        return StepResult(
            step="source_changes",
            status="completed_with_errors" if errors else "succeeded",
            fetched=summary["events"],
            updated=summary["invalidated"],
            error="; ".join(errors) if errors else None,
            extra={"opportunities": summary["opportunities"]},
        )
    except Exception as exc:  # noqa: BLE001  boundary must record any failure
        err = redact(str(exc))
        finish_run(run, IngestStats(errors=[err]), status="failed")
        logger.error("source_changes failed: %s", err)
        return StepResult(step="source_changes", status="failed", error=err)


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
    except Exception as exc:  # noqa: BLE001  boundary must record any failure
        logger.error("match failed: %s", exc)
        return StepResult(step="match", status="failed", error=str(exc))


def step_orchestrate(session: Session, settings) -> StepResult:
    """Queue the orchestrator. It does not pull SAM again; discovery records this cycle."""
    from datetime import UTC, datetime

    from govcon.bots.orchestrator import queue_orchestrator

    try:
        slot = datetime.now(UTC).strftime("%Y-%m-%dT%H")
        task, created = queue_orchestrator(
            session, settings=settings, slot=slot, pull=False, trigger="scheduler",
        )
        return StepResult(
            step="orchestrate", status="succeeded",
            extra={"task_id": task.id, "created": created},
        )
    except Exception as exc:  # noqa: BLE001  boundary must record any failure
        from govcon.logging import redact

        err = redact(str(exc))
        logger.error("orchestrate failed: %s", err)
        return StepResult(step="orchestrate", status="failed", error=err)


def step_alerts(session: Session, settings) -> StepResult:
    """Send alert digests for new matches."""
    from govcon.alerts.digest import DigestDeliveryError, run_digest

    try:
        result = run_digest(session, settings=settings, external_delivery=False)
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
    except Exception as exc:  # noqa: BLE001  boundary must record any failure
        logger.error("alerts unexpected error: %s", exc)
        return StepResult(step="alerts", status="failed", error=str(exc))


def step_usaspending(session: Session, settings) -> StepResult:
    """Pull the USAspending delta for enabled watchlist PSC/NAICS codes.

    The run is recorded under the same job name the CLI uses, so the
    incremental watermark advances after a scheduled pull.
    """
    from govcon.ingest.usaspending import ingest_usaspending_awards
    from govcon.logging import redact

    try:
        result = ingest_usaspending_awards(session, settings=settings)
    except Exception as exc:  # noqa: BLE001  boundary must record any failure
        err = redact(str(exc))
        logger.error("usaspending failed: %s", err)
        return StepResult(step="usaspending", status="failed", error=err)
    stats = result.stats
    return StepResult(
        step="usaspending",
        status=result.status,
        inserted=stats.inserted,
        updated=stats.updated,
        fetched=stats.fetched,
        unchanged=stats.unchanged,
        error="; ".join(stats.errors) if stats.errors else None,
        extra={"run_id": result.run_id, **result.details},
    )


def step_embeddings(session: Session, settings) -> StepResult:
    """Generate embeddings for opportunities that lack them."""
    try:
        from govcon.enrich.embeddings import (
            build_watchlist_profiles,
            get_default_provider,
            run_embedding_job,
        )

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
    except Exception as exc:  # noqa: BLE001  boundary must record any failure
        logger.error("embeddings failed: %s", exc)
        return StepResult(step="embeddings", status="failed", error=str(exc))


def step_semantic_match(session: Session, settings) -> StepResult:
    """Compute and persist semantic recommendations for all enabled watchlists (ADR-069).

    Records how much work was done and how long it took, so local embedding
    compute is measured rather than assumed free (roadmap §6.2).
    """
    try:
        from govcon.enrich.embeddings import get_default_provider
        from govcon.matching.recommendations import refresh_recommendations

        provider = get_default_provider(settings.embedding_model)
        stats = refresh_recommendations(session, provider, limit=20)
        return StepResult(
            step="semantic_match",
            status="succeeded",
            inserted=stats.inserted,
            updated=stats.updated,
            extra={"watchlists_processed": stats.watchlists, "deactivated": stats.deactivated,
                   "seconds": stats.seconds},
        )
    except Exception as exc:  # noqa: BLE001  boundary must record any failure
        logger.error("semantic_match failed: %s", exc)
        return StepResult(step="semantic_match", status="failed", error=str(exc))


def step_rank(session: Session) -> StepResult:
    """Rank every active match with explainable factors (ADR-069)."""
    try:
        from govcon.matching.ranking import rank_active_matches

        ranked = rank_active_matches(session)
        return StepResult(step="rank", status="succeeded", updated=ranked)
    except Exception as exc:  # noqa: BLE001  boundary must record any failure
        logger.error("rank failed: %s", exc)
        return StepResult(step="rank", status="failed", error=str(exc))


def step_auto_pursue(session: Session) -> StepResult:
    """Pursue top-ranked eligible rule matches under the owner's policy (ADR-069)."""
    try:
        from govcon.matching.auto_pursue import run_auto_pursue

        result = run_auto_pursue(session)
        if not result.enabled:
            return StepResult(step="auto_pursue", status="skipped", extra={"reason": "auto-pursue is off in Settings"})
        return StepResult(
            step="auto_pursue", status="succeeded", inserted=len(result.pursued),
            extra={"pursued_opportunity_ids": result.pursued,
                   "needs_eligibility_decision": len(result.needs_eligibility_decision),
                   "skipped_daily_cap": result.skipped_daily_cap},
        )
    except Exception as exc:  # noqa: BLE001  boundary must record any failure
        logger.error("auto_pursue failed: %s", exc)
        return StepResult(step="auto_pursue", status="failed", error=str(exc))


def step_company_registration(session: Session, settings) -> StepResult:
    """Refresh our own SAM registration daily and alert ahead of expiry (ADR-072)."""
    try:
        from govcon.company.registration import refresh_company_registration

        result = refresh_company_registration(session, settings=settings)
        extra = {
            "reason": result.reason,
            "expiration_date": result.expiration_date.isoformat() if result.expiration_date else None,
            "expiry_alert_sent": result.alerted,
            "attempt_id": result.attempt_id,
        }
        if result.status == "skipped":
            return StepResult(step="company_registration", status="skipped", extra=extra)
        if result.status == "failed":
            return StepResult(step="company_registration", status="failed", error=result.reason, extra=extra)
        if result.status == "discarded":
            return StepResult(step="company_registration", status="skipped", extra=extra)
        return StepResult(step="company_registration", status="succeeded", updated=1, extra=extra)
    except Exception as exc:  # noqa: BLE001  boundary must record any failure
        logger.error("company_registration failed: %s", exc)
        return StepResult(step="company_registration", status="failed", error=str(exc))


def step_review_escalations(session: Session, settings) -> StepResult:
    """Review reminders, overdue and deadline escalations (ADR-070)."""
    try:
        from govcon.collaboration.escalation import run_review_escalations

        counts = run_review_escalations(session, settings=settings)
        return StepResult(step="review_escalations", status="succeeded",
                          inserted=counts.reminders + counts.overdue + counts.deadline,
                          extra={"reminders": counts.reminders, "overdue": counts.overdue, "deadline": counts.deadline})
    except Exception as exc:  # noqa: BLE001  boundary must record any failure
        logger.error("review_escalations failed: %s", exc)
        return StepResult(step="review_escalations", status="failed", error=str(exc))


def step_midday_deadline_check(session: Session, settings) -> StepResult:
    """Lightweight deadline check: archive and close sweeps only (no network quota used)."""
    from govcon.ingest.lifecycle import close_expired_opportunities
    from govcon.ingest.runs import IngestStats, finish_run, start_run
    from govcon.ingest.sam_opportunities import archive_expired_sam_opportunities

    run = start_run(session, "sched:midday_check")
    try:
        stats = archive_expired_sam_opportunities(session)
        archived = stats.updated
        closed = close_expired_opportunities(session)
        stats.add(closed)
        finish_run(run, stats, status="succeeded", details={"archived": archived, "closed": closed.updated})
        return StepResult(
            step="midday_deadline_check",
            status="succeeded",
            updated=stats.updated,
            extra={"archived": archived, "closed": closed.updated},
        )
    except Exception as exc:  # noqa: BLE001  boundary must record any failure
        from govcon.ingest.runs import IngestStats
        err = str(exc)
        finish_run(run, IngestStats(errors=[err]), status="failed")
        logger.error("midday_deadline_check failed: %s", err)
        return StepResult(step="midday_deadline_check", status="failed", error=err)


def step_archive_sweep(session: Session) -> StepResult:
    """Archive SAM rows whose archive date has passed and close expired rows of every source."""
    from govcon.ingest.lifecycle import close_expired_opportunities
    from govcon.ingest.runs import IngestStats, finish_run, start_run
    from govcon.ingest.sam_opportunities import archive_expired_sam_opportunities

    run = start_run(session, "sched:archive_sweep")
    try:
        stats = archive_expired_sam_opportunities(session)
        archived = stats.updated
        closed = close_expired_opportunities(session)
        stats.add(closed)
        finish_run(run, stats, status="succeeded", details={"archived": archived, "closed": closed.updated})
        return StepResult(
            step="archive_sweep",
            status="succeeded",
            updated=stats.updated,
            extra={"archived": archived, "closed": closed.updated},
        )
    except Exception as exc:  # noqa: BLE001  boundary must record any failure
        from govcon.ingest.runs import IngestStats
        err = str(exc)
        finish_run(run, IngestStats(errors=[err]), status="failed")
        logger.error("archive_sweep failed: %s", err)
        return StepResult(step="archive_sweep", status="failed", error=err)


def step_cache_refresh(session: Session, settings) -> StepResult:
    """Refresh watchlist profile embeddings (cache refresh)."""
    try:
        from govcon.enrich.embeddings import (
            build_watchlist_profiles,
            get_default_provider,
        )

        provider = get_default_provider(settings.embedding_model)
        stats = build_watchlist_profiles(session, provider)
        return StepResult(
            step="cache_refresh",
            status="succeeded",
            extra={"watchlists_updated": stats.get("updated", 0)},
        )
    except Exception as exc:  # noqa: BLE001  boundary must record any failure
        logger.error("cache_refresh failed: %s", exc)
        return StepResult(step="cache_refresh", status="failed", error=str(exc))


def step_analytics_refresh(session: Session) -> StepResult:
    """Snapshot the outcome analytics (ADR-074).

    Reports what it counted. With no recorded outcomes there is nothing to
    refresh, and the step says skipped rather than claiming success.
    """
    try:
        import dataclasses
        import json

        from govcon.learning.analytics import outcome_analytics
        from govcon.models import AnalyticsSnapshot

        analytics = outcome_analytics(session)
        total = analytics.total_won + analytics.total_lost + analytics.total_no_bid
        if total == 0:
            return StepResult(step="analytics_refresh", status="skipped", extra={"reason": "no recorded outcomes"})
        payload = json.loads(json.dumps(dataclasses.asdict(analytics), default=str))
        session.add(AnalyticsSnapshot(outcome_count=total, won=analytics.total_won, lost=analytics.total_lost,
                                      no_bid=analytics.total_no_bid, payload=payload))
        session.flush()
        return StepResult(step="analytics_refresh", status="succeeded", inserted=1,
                          extra={"outcomes": total, "won": analytics.total_won, "lost": analytics.total_lost,
                                 "no_bid": analytics.total_no_bid})
    except Exception as exc:  # noqa: BLE001  boundary must record any failure
        logger.error("analytics_refresh failed: %s", exc)
        return StepResult(step="analytics_refresh", status="failed", error=str(exc))


def step_outcome_suggestions(session: Session, settings) -> StepResult:
    """Suggest outcomes for submitted bids from award records (ADR-073)."""
    try:
        from govcon.learning.award_matching import suggest_outcomes

        counts = suggest_outcomes(session, settings=settings)
        return StepResult(step="outcome_suggestions", status="succeeded", inserted=counts.created,
                          extra={"pursuits_checked": counts.pursuits_checked, "strong": counts.strong})
    except Exception as exc:  # noqa: BLE001  boundary must record any failure
        logger.error("outcome_suggestions failed: %s", exc)
        return StepResult(step="outcome_suggestions", status="failed", error=str(exc))


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
    except Exception as exc:  # noqa: BLE001  boundary must record any failure
        logger.error("vacuum_analyze failed: %s", exc)
        return StepResult(step="vacuum_analyze", status="failed", error=str(exc))
