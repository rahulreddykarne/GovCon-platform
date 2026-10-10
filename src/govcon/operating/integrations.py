"""Integration states from configuration and stored attempts.

A credential is not a successful call. This module never contacts SAM, DIBBS,
USAspending, or a model provider.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.config import Settings
from govcon.models import (
    AIAnalysis,
    DecisionRun,
    IngestionRun,
    MarketPriceRun,
    ProcessHeartbeat,
)
from govcon.ops.health import STALE_PROCESS, STALE_SOURCE, _embedding_check

LA = ZoneInfo("America/Los_Angeles")

NOT_CONFIGURED = "Not configured"
UNVERIFIED = "Configured but unverified"
HEALTHY = "Healthy"
DEGRADED = "Degraded"
FAILED = "Failed"
BLOCKED = "Blocked by policy"
DISABLED = "Disabled"

_SAM_JOBS = ("sched:sam_ingest", "sam_opportunities", "sam_backfill")
_DIBBS_JOBS = ("sched:dibbs_ingest", "dibbs_index")
_USA_JOBS = ("usaspending_awards",)


def integration_cards(session: Session, settings: Settings) -> list[dict[str, Any]]:
    """One card per integration. Rows are omitted when there is nothing stored."""
    return [
        _source_card(session, settings, name="SAM.gov", blurb="Federal opportunity ingestion", jobs=_SAM_JOBS, needs_key=True),
        _source_card(session, settings, name="DIBBS", blurb="DLA RFQ index and, on the ingest chain, the RFQ PDF", jobs=_DIBBS_JOBS, needs_key=False),
        _source_card(session, settings, name="USAspending", blurb="Historical awards already stored locally", jobs=_USA_JOBS, needs_key=False),
        _deepseek_card(session, settings),
        _jev_card(session, settings),
        _optional_model_card(settings, "Anthropic", settings.anthropic_api_key),
        market_price_card(session, settings),
        _optional_model_card(settings, "OpenAI", settings.openai_api_key),
        _embeddings_card(settings),
        _process_card(session, "Database", "database"),
        _process_card(session, "Worker", "worker"),
        _process_card(session, "Scheduler", "scheduler"),
        _alerts_card(settings),
    ]


def _source_card(
    session: Session, settings: Settings, *, name: str, blurb: str, jobs: tuple[str, ...], needs_key: bool,
) -> dict[str, Any]:
    rows: list[dict[str, str]] = []
    if needs_key and not settings.sam_api_key:
        rows.append({"label": "Credential", "value": "No API key in this process"})
        return _card(name, blurb, NOT_CONFIGURED, rows)
    if not needs_key:
        rows.append({"label": "Credential", "value": "Public endpoint, no key"})
    elif settings.sam_api_key:
        rows.append({"label": "Credential", "value": "Present, not printed"})
    run = _latest_ingest(session, jobs)
    if run is None:
        rows.append({"label": "Last stored run", "value": "None"})
        return _card(name, blurb, UNVERIFIED, rows)
    when = _local(run.finished_at or run.started_at)
    counts = _counts(run)
    rows.append({"label": "Last stored run", "value": f"{when} · {run.status or 'unknown'}{counts}"})
    status = _ingest_status(run)
    return _card(name, blurb, status, rows)


def _deepseek_card(session: Session, settings: Settings) -> dict[str, Any]:
    rows: list[dict[str, str]] = []
    if not settings.deepseek_api_key:
        rows.append({"label": "Credential", "value": "No API key in this process"})
        return _card("DeepSeek", "Solicitation analysis when a call is allowed", NOT_CONFIGURED, rows)
    rows.append({"label": "Credential", "value": "Present, not printed"})
    rows.append({"label": "Model id", "value": settings.deepseek_model or "deepseek-flash"})
    sharing = _sharing(settings)
    rows.append({"label": "Data-sharing policy", "value": sharing})
    latest = session.scalar(
        select(AIAnalysis).where(AIAnalysis.provider == "deepseek").order_by(AIAnalysis.created_at.desc()).limit(1)
    )
    if latest is None:
        rows.append({"label": "Last stored call", "value": "None"})
        return _card("DeepSeek", "Solicitation analysis when a call is allowed", UNVERIFIED, rows)
    quality = (latest.generation_settings or {}).get("quality") or "not recorded"
    when = _local(latest.created_at)
    latency = f" · {latest.latency_ms} ms" if latest.latency_ms is not None else ""
    rows.append({"label": "Last stored call", "value": f"{when} · {latest.model or 'model not stored'}{latency}"})
    rows.append({"label": "Output quality", "value": str(quality)})
    if quality == "incomplete":
        status = DEGRADED
    elif _stale(latest.created_at, STALE_SOURCE):
        status = DEGRADED
    else:
        status = HEALTHY
    return _card("DeepSeek", "Solicitation analysis when a call is allowed", status, rows)


def _jev_card(session: Session, settings: Settings) -> dict[str, Any]:
    rows: list[dict[str, str]] = []
    if not settings.jev_enabled:
        return _card("JEV", "Structured bid/no-bid package", DISABLED, [{"label": "Switch", "value": "JEV_ENABLED is off"}])
    if not settings.jev_api_key:
        return _card("JEV", "Structured bid/no-bid package", NOT_CONFIGURED, [{"label": "Credential", "value": "No API key in this process"}])
    rows.append({"label": "Credential", "value": "Present, not printed"})
    if not settings.ai_external_allowed_for_proprietary:
        rows.append({
            "label": "Data-sharing policy",
            "value": "Proprietary sharing is off, so a company decision package is not sent",
        })
        status = BLOCKED
    else:
        rows.append({"label": "Data-sharing policy", "value": _sharing(settings)})
        status = UNVERIFIED
    latest = session.scalar(
        select(DecisionRun).where(DecisionRun.provider == "jev").order_by(DecisionRun.created_at.desc()).limit(1)
    )
    if latest is not None:
        rows.append({"label": "Last stored JEV run", "value": f"{_local(latest.created_at)} · {latest.bundle_name}"})
        if status != BLOCKED:
            status = DEGRADED if _stale(latest.created_at, STALE_SOURCE) else HEALTHY
    else:
        rows.append({"label": "Last stored JEV run", "value": "None"})
    rules = session.scalar(
        select(DecisionRun).where(DecisionRun.provider == "rules").order_by(DecisionRun.created_at.desc()).limit(1)
    )
    if rules is not None:
        rows.append({"label": "Last rules run", "value": f"{_local(rules.created_at)} · {rules.bundle_name}"})
    return _card("JEV", "Structured bid/no-bid package, with rules as the recorded fallback", status, rows)


def _optional_model_card(settings: Settings, name: str, key: str | None) -> dict[str, Any]:
    if not key:
        return _card(name, "Optional reviewer. Not used unless a key is configured.", NOT_CONFIGURED, [
            {"label": "Credential", "value": "No API key in this process"},
            {"label": "Last stored call", "value": "None"},
        ])
    return _card(name, "Optional reviewer.", UNVERIFIED, [
        {"label": "Credential", "value": "Present, not printed"},
        {"label": "Data-sharing policy", "value": _sharing(settings)},
        {"label": "Last stored call", "value": "Not queried; this page does not assume a call happened"},
    ])


MARKET_PRICE_CARD = "Claude web search"
_MARKET_PRICE_BLURB = "Web market prices for pursued product opportunities; government sites excluded"


def market_price_card(session: Session, settings: Settings) -> dict[str, Any]:
    """Health from the last stored search that reached the provider; skipped runs made no call."""
    limits = (f"{settings.market_price_max_results} prices, {settings.market_price_max_searches} searches, "
              f"{settings.market_price_max_fetches} page reads per opportunity")
    if not settings.market_price_research_enabled:
        return _card(MARKET_PRICE_CARD, _MARKET_PRICE_BLURB, DISABLED,
                     [{"label": "Switch", "value": "MARKET_PRICE_RESEARCH_ENABLED is off"}])
    if not settings.anthropic_api_key:
        return _card(MARKET_PRICE_CARD, _MARKET_PRICE_BLURB, NOT_CONFIGURED,
                     [{"label": "Credential", "value": "No ANTHROPIC_API_KEY in this process"}])
    rows = [
        {"label": "Credential", "value": "Present, not printed"},
        {"label": "Model id", "value": settings.market_price_model or settings.anthropic_model or "provider default"},
        {"label": "Limits", "value": limits},
        {"label": "Data-sharing policy", "value": _sharing(settings)},
    ]
    latest = session.scalar(
        select(MarketPriceRun).where(MarketPriceRun.status.in_(("completed", "no_results", "failed", "blocked")))
        .order_by(MarketPriceRun.created_at.desc(), MarketPriceRun.id.desc()).limit(1)
    )
    if latest is None:
        rows.append({"label": "Last stored search", "value": "None"})
        return _card(MARKET_PRICE_CARD, _MARKET_PRICE_BLURB, UNVERIFIED, rows)
    searches = (latest.usage or {}).get("web_search_requests")
    rows.append({"label": "Last stored search", "value": (
        f"{_local(latest.created_at)} · opportunity #{latest.opportunity_id} · {latest.status.replace('_', ' ')}"
        + (f" · {searches} search(es)" if searches is not None else "")
    )})
    if latest.estimate_unit_cost is not None:
        rows.append({"label": "Estimate", "value": f"${latest.estimate_unit_cost:,.2f} per {latest.unit or 'unit'} "
                                                   f"from {len(latest.listings)} listing(s)"})
    elif latest.note:
        rows.append({"label": "Note", "value": latest.note[:200]})
    status = {"completed": HEALTHY, "no_results": HEALTHY, "failed": FAILED, "blocked": BLOCKED}[latest.status]
    return _card(MARKET_PRICE_CARD, _MARKET_PRICE_BLURB, status, rows)


def _embeddings_card(settings: Settings) -> dict[str, Any]:
    check = _embedding_check(settings)
    status = HEALTHY if check["status"] == "ok" else DEGRADED
    return _card("Local embeddings", "Semantic matching on this machine. No text is sent out.", status, [
        {"label": "Check", "value": check["detail"]},
        {"label": "External data sent", "value": "None"},
    ])


def _process_card(session: Session, title: str, role: str) -> dict[str, Any]:
    if role == "database":
        return _card("Database", "Postgres used by this process", HEALTHY, [
            {"label": "Check", "value": "This page loaded, so the database answered"},
        ])
    latest = session.scalar(
        select(ProcessHeartbeat).where(ProcessHeartbeat.role == role).order_by(ProcessHeartbeat.beat_at.desc()).limit(1)
    )
    if latest is None:
        return _card(title, "Local process heartbeat", FAILED, [{"label": "Heartbeat", "value": "None stored"}])
    age = datetime.now(UTC) - _aware(latest.beat_at)
    when = _local(latest.beat_at)
    if age > STALE_PROCESS:
        return _card(title, "Local process heartbeat", DEGRADED, [{"label": "Last heartbeat", "value": f"{when} · older than 90 seconds"}])
    return _card(title, "Local process heartbeat", HEALTHY, [{"label": "Last heartbeat", "value": when}])


def _alerts_card(settings: Settings) -> dict[str, Any]:
    if settings.email_configured:
        return _card("Alert delivery", "Digest transport", UNVERIFIED, [
            {"label": "SMTP", "value": "Configured. This page does not send a message."},
            {"label": "Application default", "value": "Scheduled digests still write the outbox"},
        ])
    return _card("Alert delivery", "Digest transport", NOT_CONFIGURED, [
        {"label": "SMTP", "value": "Not configured"},
        {"label": "What runs today", "value": "HTML files in the outbox. Nothing is emailed."},
    ])


def _card(name: str, blurb: str, status: str, rows: list[dict[str, str]]) -> dict[str, Any]:
    return {"name": name, "blurb": blurb, "status": status, "tag": _tag(status), "rows": rows}


def _tag(status: str) -> str:
    if status == HEALTHY:
        return "good"
    if status in {DEGRADED, UNVERIFIED}:
        return "warn"
    if status == FAILED:
        return "bad"
    return "info"


def _latest_ingest(session: Session, jobs: tuple[str, ...]) -> IngestionRun | None:
    return session.scalar(
        select(IngestionRun).where(IngestionRun.job.in_(jobs)).order_by(IngestionRun.started_at.desc()).limit(1)
    )


def _ingest_status(run: IngestionRun) -> str:
    if run.status == "failed":
        return FAILED
    if run.status == "completed_with_errors":
        return DEGRADED
    if run.status == "succeeded":
        return DEGRADED if _stale(run.finished_at or run.started_at, STALE_SOURCE) else HEALTHY
    return UNVERIFIED


def _counts(run: IngestionRun) -> str:
    parts = []
    if run.fetched:
        parts.append(f"{run.fetched} fetched")
    if run.inserted:
        parts.append(f"{run.inserted} inserted")
    if run.updated:
        parts.append(f"{run.updated} updated")
    return " · " + ", ".join(parts) if parts else ""


def _sharing(settings: Settings) -> str:
    return (
        f"proprietary {'on' if settings.ai_external_allowed_for_proprietary else 'off'}, "
        f"FCI {'on' if settings.ai_external_allowed_for_fci else 'off'}, "
        f"CUI {'on' if settings.ai_external_allowed_for_cui else 'off'}"
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _stale(value: datetime | None, limit: timedelta) -> bool:
    if value is None:
        return True
    return datetime.now(UTC) - _aware(value) > limit


def _local(value: datetime | None) -> str:
    if value is None:
        return "time not stored"
    return _aware(value).astimezone(LA).strftime("%Y-%m-%d %I:%M %p PT")
