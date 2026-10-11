"""Read-only control-center projections. No provider calls or document contents."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from govcon.config import Settings
from govcon.display_time import format_pt
from govcon.logging import redact
from govcon.models import (
    AIProviderCall,
    BotRun,
    FilePage,
    SchedulerJobRun,
    StoredFile,
    Task,
)
from govcon.ops.health import chain_board, collect_health

PAGE_SIZE = 30


def _duration(start: datetime | None, end: datetime | None) -> str:
    if start is None:
        return "Not started"
    start = start.replace(tzinfo=UTC) if start.tzinfo is None else start
    if end is not None and end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    seconds = max(0, int(((end or datetime.now(UTC)) - start).total_seconds()))
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}m {seconds}s" if minutes else f"{seconds}s"


def _task(row: Task) -> dict[str, Any]:
    return {
        "id": row.id, "name": row.task_type.replace("_", " "), "status": row.status,
        "step": row.current_step or "No step recorded", "opportunity_id": row.opportunity_id,
        "started": format_pt(row.started_at), "created": format_pt(row.created_at),
        "duration": _duration(row.started_at, row.finished_at),
        "attempts": row.attempts, "max_attempts": row.max_attempts,
        "error": redact(row.last_error or ""), "error_type": row.last_error_type,
        "next_action": redact(row.blocker_next_action or ""),
        "owner": row.blocker_owner_role or "Not assigned",
        "next_attempt": format_pt(row.next_attempt_at),
        "lease_expired": row.status == "running" and row.lease_expires_at is not None
        and (row.lease_expires_at.replace(tzinfo=UTC) if row.lease_expires_at.tzinfo is None
             else row.lease_expires_at) < datetime.now(UTC),
    }


def _run(row: SchedulerJobRun) -> dict[str, Any]:
    return {
        "id": row.id, "name": row.chain_name, "trigger": row.trigger, "status": row.status,
        "started": format_pt(row.started_at), "finished": format_pt(row.finished_at),
        "duration": _duration(row.started_at, row.finished_at),
        "steps": row.steps_completed or [], "failed_step": row.failed_step,
        "error": redact(row.error or ""),
    }


def _paged(session: Session, query, page: int) -> tuple[list, int, int]:
    total = int(session.scalar(select(func.count()).select_from(query.subquery())) or 0)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(page, 1), pages)
    rows = list(session.scalars(query.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)).all())
    return rows, page, pages


def control_center(session: Session, settings: Settings, *, view: str,
                   page: int = 1, file_id: int | None = None, chain: str | None = None) -> dict[str, Any]:
    """Bounded history pages, aggregate counters, and explicitly recorded activity."""
    now = datetime.now(UTC)
    health = collect_health(session, settings)
    task_counts = dict(session.execute(select(Task.status, func.count()).group_by(Task.status)).all())
    file_counts = dict(session.execute(select(StoredFile.extraction_status, func.count())
                                      .where(StoredFile.active.is_(True))
                                      .group_by(StoredFile.extraction_status)).all())
    calls = session.execute(select(
        func.count(AIProviderCall.id), func.sum(AIProviderCall.cost_usd),
        func.count(AIProviderCall.id).filter(AIProviderCall.cost_usd.is_(None)),
    ).where(AIProviderCall.created_at >= now - timedelta(days=30),
            AIProviderCall.status != "local")).one()
    chains = chain_board(session)
    chain_counts = {name: int(count) for name, count in session.execute(
        select(SchedulerJobRun.chain_name, func.count()).group_by(SchedulerJobRun.chain_name)
    )}
    for item in chains:
        item["run_count"] = chain_counts.get(item["name"], 0)
        item["failure_error"] = redact(item["last_failure"].error or "") if item["last_failure"] else ""
    result: dict[str, Any] = {
        "updated": format_pt(now, seconds=True), "updated_iso": now.isoformat(),
        "health": health["checks"], "task_counts": task_counts, "file_counts": file_counts,
        "running": int(task_counts.get("running", 0)),
        "queued": int(task_counts.get("queued", 0)) + int(task_counts.get("retrying", 0)),
        "blocked": sum(int(task_counts.get(key, 0)) for key in ("failed", "waiting_for_input", "waiting_for_budget")),
        "file_total": sum(file_counts.values()),
        "call_count": int(calls[0]), "known_cost": f"${calls[1]:,.4f}" if calls[1] is not None else None,
        "unknown_cost_count": int(calls[2]), "chains": chains, "schedule_total": sum(chain_counts.values()),
        "chain_filter": chain or "", "page": 1, "pages": 1,
        "tasks": [], "runs": [], "files": [], "selected_file": None, "file_missing": False,
        "file_pages": [], "providers": [], "recent_calls": [],
    }
    if view in {"overview", "activity"}:
        query = select(Task).order_by(Task.created_at.desc(), Task.id.desc())
        if view == "overview":
            query = query.where(Task.status.in_(("running", "queued", "retrying", "waiting_for_input", "waiting_for_budget", "failed")))
            result["tasks"] = [_task(row) for row in session.scalars(query.limit(8))]
        else:
            rows, result["page"], result["pages"] = _paged(session, query, page)
            result["tasks"] = [_task(row) for row in rows]
        result["bot_activity"] = [{"name": row.bot_name.replace("_", " "), "status": row.status,
                                   "started": format_pt(row.started_at), "opportunity_id": row.opportunity_id,
                                   "error": redact(row.error or ""), "attempt": row.attempt}
                                  for row in session.scalars(select(BotRun).order_by(BotRun.started_at.desc()).limit(10))]
    if view in {"overview", "schedules"}:
        run_query = select(SchedulerJobRun).order_by(SchedulerJobRun.started_at.desc(), SchedulerJobRun.id.desc())
        if chain:
            run_query = run_query.where(SchedulerJobRun.chain_name == chain)
        if view == "schedules":
            rows, result["page"], result["pages"] = _paged(session, run_query, page)
        else:
            rows = list(session.scalars(run_query.limit(5)))
        result["runs"] = [_run(row) for row in rows]
    if view == "documents":
        rows, result["page"], result["pages"] = _paged(session, select(StoredFile)
            .order_by(StoredFile.created_at.desc(), StoredFile.id.desc()), page)
        result["files"] = [_file(row) for row in rows]
        chosen = session.get(StoredFile, file_id) if file_id is not None else (rows[0] if rows else None)
        result["file_missing"] = file_id is not None and chosen is None
        if chosen:
            result["selected_file"] = _file(chosen)
            # Page text is intentionally not copied into a global operations screen.
            result["file_pages"] = [{"number": row.page_no, "label": row.label,
                                     "method": row.text_source, "characters": row.char_count,
                                     "confidence": str(row.ocr_confidence) if row.ocr_confidence is not None else None}
                                    for row in session.scalars(select(FilePage).where(FilePage.file_id == chosen.id)
                                                             .order_by(FilePage.page_no).limit(100))]
            result["page_records"] = int(session.scalar(select(func.count()).select_from(FilePage)
                                                        .where(FilePage.file_id == chosen.id)) or 0)
    if view in {"overview", "integrations"}:
        # Actual attempt telemetry complements configuration/health cards.
        grouped = session.execute(select(AIProviderCall.provider, AIProviderCall.status, func.count())
                                  .group_by(AIProviderCall.provider, AIProviderCall.status)).all()
        providers: dict[str, dict[str, Any]] = {}
        for provider, status, count in grouped:
            item = providers.setdefault(provider, {"name": provider, "calls": 0, "statuses": {}})
            item["calls"] += int(count)
            item["statuses"][status] = int(count)
        for name in ("deepseek", "jev", "anthropic", "openai"):
            providers.setdefault(name, {"name": name, "calls": 0, "statuses": {}})
        result["providers"] = list(providers.values())
        result["recent_calls"] = [{"provider": row.provider, "model": row.model or "Not reported",
                                   "purpose": row.purpose, "status": row.status,
                                   "when": format_pt(row.created_at, seconds=True),
                                   "latency": f"{row.latency_ms:,} ms" if row.latency_ms is not None else "Not reported",
                                   "opportunity_id": row.opportunity_id}
                                  for row in session.scalars(select(AIProviderCall)
                                  .where(AIProviderCall.status != "local")
                                  .order_by(AIProviderCall.created_at.desc(), AIProviderCall.id.desc()).limit(12))]
    return result


def _file(row: StoredFile) -> dict[str, Any]:
    return {
        "id": row.id, "name": row.filename or "Unnamed file", "opportunity_id": row.opportunity_id,
        "status": row.extraction_status or "not run", "classification": row.classification,
        "origin": row.source_origin, "active": row.active, "pages": row.page_count,
        "ocr_pages": row.ocr_pages or [], "unread_pages": row.ocr_failed_pages or [],
        "downloaded": format_pt(row.downloaded_at), "hash": row.sha256,
        "error": redact(row.extraction_error or ""), "type": row.mime_type or "Not recorded",
    }
