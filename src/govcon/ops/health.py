"""Health signals that do not call external services or print secrets.

Source APIs and AI providers are judged from local configuration and the
last stored run. A page load never contacts SAM, DIBBS, USAspending, or a
model provider.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from govcon.config import Settings, get_settings
from govcon.models import IngestionRun, ProcessHeartbeat, SchedulerJobRun
from govcon.scheduler.chains import CHAIN_DEFINITIONS
from govcon.scheduler.schedule import OPERATOR_TZ_NAME, describe

STALE_PROCESS = timedelta(seconds=90)
STALE_SOURCE = timedelta(hours=36)

_SOURCE_JOBS: dict[str, tuple[str, ...]] = {
    "sam": ("sched:sam_ingest", "sam_opportunities", "sam_backfill"),
    "dibbs": ("sched:dibbs_ingest", "dibbs_index"),
    "usaspending": ("usaspending_awards",),
}


def record_heartbeat(
    session: Session,
    *,
    role: str,
    instance_id: str,
    detail: dict[str, Any] | None = None,
) -> None:
    """Upsert one process heartbeat. ``detail`` must not contain secrets."""
    now = datetime.now(UTC)
    payload = detail or {}
    stmt = pg_insert(ProcessHeartbeat).values(
        role=role,
        instance_id=instance_id,
        beat_at=now,
        detail=payload,
    ).on_conflict_do_update(
        constraint="uq_process_heartbeats_role_instance",
        set_={"beat_at": now, "detail": payload},
    )
    session.execute(stmt)


def collect_health(session: Session, settings: Settings | None = None) -> dict[str, Any]:
    """Return component checks. Database failure is the caller's problem."""
    settings = settings or get_settings()
    checks: list[dict[str, str]] = []
    started = datetime.now(UTC)
    session.execute(text("SELECT 1"))
    elapsed_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
    checks.append({
        "name": "database",
        "status": "ok",
        "detail": f"reachable ({elapsed_ms} ms)",
    })
    checks.append(_process_check(session, "web", "web"))
    checks.append(_process_check(session, "worker", "worker"))
    checks.append(_process_check(session, "scheduler", "scheduler"))
    checks.extend(_source_checks(session))
    checks.append(_embedding_check(settings))
    checks.append(_provider_check(settings))
    return {"status": "ok", "checks": checks}


def chain_board(session: Session) -> list[dict[str, Any]]:
    """One row per chain: last success, last failure, next run, duration, counts."""
    from govcon.scheduler.schedule import effective_jobs
    from govcon.workflow.app_settings import OPERATOR_SCHEDULE, get_setting

    saved = get_setting(session, OPERATOR_SCHEDULE)
    jobs = effective_jobs(saved.get("jobs") if isinstance(saved, dict) else None)
    upcoming = _next_runs(session)
    rows: list[dict[str, Any]] = []
    for name, chain_def in CHAIN_DEFINITIONS.items():
        last = _latest(session, name, statuses=None)
        last_ok = _latest(session, name, statuses=("succeeded", "completed_with_errors"))
        last_bad = _latest(session, name, statuses=("failed",))
        duration = _duration(last)
        rows.append({
            "name": name,
            "description": chain_def.description,
            "cron": describe(name, jobs) if name in jobs else chain_def.cron,
            "steps": chain_def.steps,
            "last_run": last,
            "last_success": last_ok,
            "last_failure": last_bad,
            "next_run_local": _format_local(upcoming.get(name)),
            "duration_text": duration,
            "counts_text": _counts(last),
            "action": _action(name, last, last_bad),
        })
    return rows


def _process_check(session: Session, role: str, label: str) -> dict[str, str]:
    latest = session.scalar(
        select(ProcessHeartbeat)
        .where(ProcessHeartbeat.role == role)
        .order_by(ProcessHeartbeat.beat_at.desc())
        .limit(1)
    )
    if latest is None:
        return {
            "name": label,
            "status": "down",
            "detail": f"no heartbeat — start `govcon {role} start`" if role != "web" else "no heartbeat yet",
        }
    age = datetime.now(UTC) - _aware(latest.beat_at)
    if age > STALE_PROCESS:
        minutes = int(age.total_seconds() // 60)
        return {"name": label, "status": "down", "detail": f"last heartbeat {minutes} min ago"}
    return {"name": label, "status": "ok", "detail": f"heartbeat {int(age.total_seconds())}s ago"}


def _source_checks(session: Session) -> list[dict[str, str]]:
    checks = []
    now = datetime.now(UTC)
    for name, jobs in _SOURCE_JOBS.items():
        last_ok = session.scalar(
            select(IngestionRun)
            .where(IngestionRun.job.in_(jobs), IngestionRun.status.in_(("succeeded", "completed_with_errors")))
            .order_by(IngestionRun.started_at.desc())
            .limit(1)
        )
        last_bad = session.scalar(
            select(IngestionRun)
            .where(IngestionRun.job.in_(jobs), IngestionRun.status == "failed")
            .order_by(IngestionRun.started_at.desc())
            .limit(1)
        )
        if last_ok is None:
            detail = "no successful local run yet"
            status = "unknown"
        else:
            when = _aware(last_ok.finished_at or last_ok.started_at)
            age = now - when
            status = "stale" if age > STALE_SOURCE else "ok"
            detail = f"last success {when.astimezone(UTC).strftime('%Y-%m-%d %H:%M')} UTC"
        if last_bad is not None:
            failed_at = _aware(last_bad.started_at)
            detail = f"{detail}; last failure {failed_at.strftime('%Y-%m-%d %H:%M')} UTC"
            if status == "ok" and last_ok is not None and _aware(last_ok.started_at) < failed_at:
                status = "degraded"
        checks.append({
            "name": f"source:{name}",
            "status": status,
            "detail": detail + " (from local history, not a live call)",
        })
    return checks


def _embedding_check(settings: Settings) -> dict[str, str]:
    name = settings.embedding_model or "all-MiniLM-L6-v2"
    cached = False
    try:
        from pathlib import Path

        if Path(name).is_dir():
            cached = True
        else:
            from huggingface_hub import try_to_load_from_cache

            repo = name if "/" in name else f"sentence-transformers/{name}"
            found = try_to_load_from_cache(repo, "config.json")
            cached = isinstance(found, str)
    except Exception:
        cached = False
    if cached:
        return {"name": "embeddings", "status": "ok", "detail": f"{name} is cached for local use"}
    return {
        "name": "embeddings",
        "status": "degraded",
        "detail": f"{name} is not cached; the first embedding job may download it",
    }


def _provider_check(settings: Settings) -> dict[str, str]:
    configured: list[str] = []
    if settings.deepseek_api_key:
        configured.append("deepseek")
    if settings.anthropic_api_key:
        configured.append("anthropic")
    if settings.openai_api_key:
        configured.append("openai")
    if settings.jev_enabled and settings.jev_api_key:
        configured.append("jev")
    flags = (
        f"proprietary={'on' if settings.ai_external_allowed_for_proprietary else 'off'}, "
        f"fci={'on' if settings.ai_external_allowed_for_fci else 'off'}, "
        f"cui={'on' if settings.ai_external_allowed_for_cui else 'off'}"
    )
    names = ", ".join(configured) if configured else "none"
    return {
        "name": "ai_providers",
        "status": "ok" if configured else "unknown",
        "detail": f"configured: {names}. External sharing {flags}. No provider was contacted.",
    }


def _latest(session: Session, chain_name: str, statuses: tuple[str, ...] | None) -> SchedulerJobRun | None:
    query = select(SchedulerJobRun).where(SchedulerJobRun.chain_name == chain_name)
    if statuses is not None:
        query = query.where(SchedulerJobRun.status.in_(statuses))
    return session.scalar(query.order_by(SchedulerJobRun.started_at.desc()).limit(1))


def _next_runs(session: Session) -> dict[str, datetime | None]:
    try:
        with session.begin_nested():
            rows = session.execute(text("SELECT id, next_run_time FROM apscheduler_jobs")).all()
    except Exception:
        return {}
    found: dict[str, datetime | None] = {}
    for job_id, raw in rows:
        if raw is None:
            found[str(job_id)] = None
            continue
        found[str(job_id)] = datetime.fromtimestamp(float(raw), UTC)
    return found


def _duration(run: SchedulerJobRun | None) -> str:
    if run is None or run.started_at is None or run.finished_at is None:
        return "—"
    seconds = (_aware(run.finished_at) - _aware(run.started_at)).total_seconds()
    return f"{seconds:.0f}s"


def _counts(run: SchedulerJobRun | None) -> str:
    if run is None or not run.row_counts:
        return "—"
    totals: dict[str, int] = {}
    for step_counts in run.row_counts.values():
        if not isinstance(step_counts, dict):
            continue
        for key, value in step_counts.items():
            totals[key] = totals.get(key, 0) + int(value or 0)
    if not totals:
        return "—"
    return " ".join(f"{key}={value}" for key, value in totals.items())


def _action(name: str, last: SchedulerJobRun | None, last_bad: SchedulerJobRun | None) -> str:
    if last is not None and last.status == "running":
        return "A run is in progress. Wait, or check the worker log if this stays running."
    if last_bad is not None and (last is None or last.id == last_bad.id or (
        last.started_at is not None and last_bad.started_at is not None
        and _aware(last_bad.started_at) >= _aware(last.started_at)
    )):
        step = last_bad.failed_step or "a step"
        return f"{name} failed at {step}. Read the error on this page, fix it, then run `govcon jobs run {name}`."
    if last is None:
        return f"Not run yet. Start the scheduler, or run `govcon jobs run {name}`."
    return "No action."


def _format_local(moment: datetime | None) -> str:
    if moment is None:
        return "not scheduled (scheduler has not saved a next run)"
    from zoneinfo import ZoneInfo

    local = _aware(moment).astimezone(ZoneInfo(OPERATOR_TZ_NAME))
    return local.strftime("%Y-%m-%d %I:%M %p PT")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value
