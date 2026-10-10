"""Stage-by-stage trace for one stored opportunity.

Every stage is listed. A stage with no row is "not run" or "not stored".
This module does not start work and does not call an external API.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from govcon.ai.analysis_types import AnalysisType
from govcon.bots.catalog import CATALOG
from govcon.models import (
    AIAnalysis,
    BidDecision,
    BotApproval,
    BotRun,
    ComplianceRun,
    MarketPriceRun,
    Opportunity,
    Requirement,
    StoredFile,
)

LA = ZoneInfo("America/Los_Angeles")

# The order the orchestrator uses for one notice, then the human gate and the analysis row.
_BOTS = ("document", "matching", "compliance", "awards", "amendment", "bid_decision")


def opportunity_trace(session: Session, opportunity: Opportunity) -> list[dict[str, Any]]:
    runs = _latest_runs(session, opportunity.id)
    approval = session.scalar(
        select(BotApproval).where(BotApproval.opportunity_id == opportunity.id)
        .order_by(BotApproval.requested_at.desc()).limit(1)
    )
    from govcon.enrich.summarize import latest_solicitation_summary

    analysis = latest_solicitation_summary(session, opportunity.id)
    files = list(session.scalars(
        select(StoredFile).where(StoredFile.opportunity_id == opportunity.id, StoredFile.active.is_(True))
    ).all())
    market = session.scalar(
        select(MarketPriceRun).where(MarketPriceRun.opportunity_id == opportunity.id)
        .order_by(MarketPriceRun.created_at.desc(), MarketPriceRun.id.desc()).limit(1)
    )
    matrix = session.scalar(
        select(ComplianceRun).where(
            ComplianceRun.opportunity_id == opportunity.id,
            ComplianceRun.run_type == "compliance_matrix",
        ).order_by(ComplianceRun.created_at.desc(), ComplianceRun.id.desc()).limit(1)
    )
    decision = session.scalar(
        select(BidDecision).where(BidDecision.opportunity_id == opportunity.id)
        .order_by(BidDecision.created_at.desc()).limit(1)
    )
    document_analysis = session.scalar(
        select(AIAnalysis).where(
            AIAnalysis.opportunity_id == opportunity.id,
            AIAnalysis.analysis_type.in_((
                AnalysisType.SOLICITATION_SUMMARY,
                AnalysisType.COMPLIANCE_REVIEW,
            )),
        ).order_by(AIAnalysis.created_at.desc(), AIAnalysis.id.desc()).limit(1)
    )
    requirement_count = int(session.scalar(
        select(func.count()).select_from(Requirement).where(
            Requirement.opportunity_id == opportunity.id,
            Requirement.status != "superseded",
        )
    ) or 0)
    stages = [_notice(opportunity), _files(files)]
    for name in _BOTS:
        if name == "document":
            stages.append(_document_stage(runs.get(name), document_analysis, requirement_count))
        elif name == "compliance":
            stages.append(_compliance_stage(runs.get(name), matrix))
            stages.append(_market_prices(market))
        elif name == "bid_decision":
            stages.append(_bid_stage(runs.get(name), decision))
        else:
            stages.append(_bot(name, runs.get(name)))
    stages.append(_human(approval))
    stages.append(_analysis(analysis))
    return stages


def _document_stage(
    run: BotRun | None, analysis: AIAnalysis | None, requirement_count: int,
) -> dict[str, Any]:
    if analysis is not None:
        when = _local(analysis.created_at)
        detail = f"Analysis {analysis.id}. {when}."
        if requirement_count:
            detail = f"{requirement_count} requirement(s) stored. {detail}"
        return _stage("document", "Document", "stored", "good", detail)
    if requirement_count:
        return _stage("document", "Document", "stored", "good",
                      f"{requirement_count} requirement(s) stored.")
    return _bot("document", run)


def _compliance_stage(run: BotRun | None, matrix: ComplianceRun | None) -> dict[str, Any]:
    if matrix is not None:
        when = _local(matrix.created_at)
        status = matrix.status or "stored"
        tag = "good" if status == "complete" else ("warn" if status == "incomplete" else "info")
        return _stage("compliance", "Compliance", status.replace("_", " "), tag,
                      f"Matrix run {matrix.id}. {when}.")
    return _bot("compliance", run)


def _bid_stage(run: BotRun | None, decision: BidDecision | None) -> dict[str, Any]:
    if decision is not None:
        when = _local(decision.created_at)
        rec = (decision.recommendation or "stored").replace("_", " ")
        return _stage("bid_decision", "Bid / no-bid", rec, "good",
                      f"Decision {decision.id}. {when}.")
    return _bot("bid_decision", run)


def _notice(opportunity: Opportunity) -> dict[str, Any]:
    when = _local(opportunity.updated_at or opportunity.created_at)
    return _stage(
        "notice",
        "Notice stored",
        "stored",
        "good",
        f"{opportunity.source} {opportunity.source_id}. Last update {when}.",
    )


def _files(files: list[StoredFile]) -> dict[str, Any]:
    stored = [row for row in files if row.sha256]
    missing = [row for row in files if not row.sha256]
    if not files:
        return _stage("files", "Source files", "not stored", "info", "No active file is stored for this notice.")
    if missing and not stored:
        return _stage(
            "files", "Source files", "not downloaded", "bad",
            f"{len(missing)} file row(s) have no stored bytes.",
        )
    detail = f"{len(stored)} file(s) stored"
    if missing:
        detail += f", {len(missing)} without bytes"
    tag = "warn" if missing else "good"
    return _stage("files", "Source files", "stored" if not missing else "partial", tag, detail + ".")


def _bot(name: str, run: BotRun | None) -> dict[str, Any]:
    title = CATALOG[name]["title"]
    if run is None:
        return _stage(name, title, "not run", "info", "No run is stored for this notice.")
    detail = run.error or (run.outputs or {}).get("state") or run.status
    when = _local(run.finished_at or run.started_at)
    incomplete = (run.outputs or {}).get("state") == "incomplete"
    if run.status in {"failed", "blocked"} or incomplete:
        tag = "bad"
        status = "incomplete" if incomplete and run.status == "succeeded" else run.status
    elif run.status in {"waiting_approval", "completed_with_errors", "running"}:
        tag = "warn"
        status = run.status
    elif run.status == "succeeded":
        tag = "good"
        status = run.status
    else:
        tag = "info"
        status = run.status
    return _stage(name, title, status.replace("_", " "), tag, f"Attempt {run.attempt}. {when}. {detail}")


_MARKET_TAGS = {"completed": "good", "no_results": "info", "skipped": "info", "blocked": "warn", "failed": "bad"}


def _market_prices(run: MarketPriceRun | None) -> dict[str, Any]:
    from govcon.sourcing.market_prices import run_summary

    if run is None:
        return _stage("market_prices", "Market prices", "not run", "info",
                      "No web price search is stored for this notice. Preparation runs it for pursued products.")
    return _stage("market_prices", "Market prices", run.status.replace("_", " "), _MARKET_TAGS.get(run.status, "info"),
                  f"{_local(run.created_at)}. {run_summary(run)}")


def _human(approval: BotApproval | None) -> dict[str, Any]:
    if approval is None:
        return _stage("human", "Your decision", "not requested", "info", "No bot approval is stored for this notice.")
    if approval.status == "pending":
        return _stage("human", "Your decision", "waiting for you", "warn", approval.summary)
    when = _local(approval.decided_at)
    return _stage("human", "Your decision", approval.status, "good" if approval.status == "approved" else "info", f"{approval.summary} {when}")


def _analysis(row: AIAnalysis | None) -> dict[str, Any]:
    if row is None:
        return _stage("analysis", "Solicitation analysis", "not run", "info", "No analysis row is stored.")
    quality = (row.generation_settings or {}).get("quality") or "not recorded"
    reason = (row.generation_settings or {}).get("quality_reason") or ""
    if quality == "incomplete":
        return _stage("analysis", "Solicitation analysis", "incomplete", "bad", reason or "The stored output is not finished.")
    model = row.model or "model not stored"
    return _stage("analysis", "Solicitation analysis", str(quality), "good" if quality == "accepted" else "info", f"{row.provider or 'provider not stored'} · {model}")


def _latest_runs(session: Session, opportunity_id: int) -> dict[str, BotRun]:
    rows = session.scalars(
        select(BotRun).where(BotRun.opportunity_id == opportunity_id, BotRun.bot_name.in_(_BOTS))
        .order_by(BotRun.started_at.desc(), BotRun.id.desc())
    ).all()
    found: dict[str, BotRun] = {}
    for row in rows:
        found.setdefault(row.bot_name, row)
    return found


def _stage(stage_id: str, title: str, status: str, tag: str, detail: str) -> dict[str, Any]:
    return {"id": stage_id, "title": title, "status": status, "tag": tag, "detail": str(detail)[:400]}


def _local(value: datetime | None) -> str:
    if value is None:
        return "time not stored"
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(LA).strftime("%Y-%m-%d %I:%M %p PT")
