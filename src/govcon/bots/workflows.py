"""Bot work. Network pulls go through the existing ingest and download functions."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.bots.store import begin_run, finish_run, open_approval
from govcon.config import Settings
from govcon.logging import redact
from govcon.models import (
    BotRun,
    IngestionRun,
    Opportunity,
    OpportunityEvent,
    Requirement,
)

_SHALL = re.compile(r"\b(shall|must|required)\b", re.IGNORECASE)
_IMPACT = {
    "created": "The notice was stored for the first time.",
    "deadline_changed": "The response deadline changed. Confirm the date on the source before acting.",
    "cancelled": "The source marked this notice cancelled.",
    "files_added": "The source added files. They are not reviewed until the document bot reads them.",
    "files_removed": "The source removed files. Older extracts may be stale.",
    "set_aside_changed": "The set-aside changed. Recheck eligibility.",
    "description_changed": "The description text changed.",
    "quantity_changed": "The quantity changed.",
    "material_source_change": "The source changed in a way the ingest job marked material.",
}


def pull_sources(session: Session, settings: Settings) -> tuple[datetime, dict[str, Any]]:
    """SAM and DIBBS through the scheduler steps. Tests replace this."""
    from govcon.scheduler.jobs import step_dibbs_ingest, step_sam_ingest

    started = datetime.now(UTC)
    sam = step_sam_ingest(session, settings)
    dibbs = step_dibbs_ingest(session, settings)
    return started, {"sam": _step_public(sam), "dibbs": _step_public(dibbs)}


def execute_discovery(
    session: Session,
    settings: Settings,
    *,
    slot: str,
    trigger: str,
    parent_run_id: int | None,
    pull: bool,
) -> BotRun:
    mode = "pull" if pull else "observe"
    run, started = begin_run(
        session, bot_name="discovery", idempotency_key=f"discovery:{slot}:{mode}",
        trigger=trigger, parent_run_id=parent_run_id,
        inputs={"slot": slot, "pull": pull},
    )
    if not started:
        return run
    try:
        if pull:
            started_at, payload = pull_sources(session, settings)
            ids = _ids_since(session, started_at)
        else:
            payload, ids = _observed_ingest(session)
        statuses = [payload["sam"]["status"], payload["dibbs"]["status"]]
        if all(item == "failed" for item in statuses):
            status = "failed"
        elif any(item in {"failed", "completed_with_errors", "skipped"} for item in statuses):
            status = "completed_with_errors"
        else:
            status = "succeeded"
        error = "; ".join(
            item["error"] for item in (payload["sam"], payload["dibbs"]) if item.get("error")
        ) or None
        finish_run(run, status, error=error, outputs={
            "opportunity_ids": ids,
            "sam": payload["sam"],
            "dibbs": payload["dibbs"],
            "analysis_complete": False,
            "state": "incomplete" if status == "failed" else "recorded",
        })
    except Exception as exc:
        finish_run(run, "failed", error=redact(str(exc)), outputs={
            "opportunity_ids": [], "analysis_complete": False, "state": "incomplete",
        })
    return run


def execute_document(
    session: Session, settings: Settings, *, opportunity_id: int, trigger: str, parent_run_id: int | None,
) -> BotRun:
    revision = _revision(session, opportunity_id)
    run, started = begin_run(
        session, bot_name="document", idempotency_key=f"document:{opportunity_id}:{revision}",
        trigger=trigger, opportunity_id=opportunity_id, source_revision=revision,
        parent_run_id=parent_run_id, inputs={"source_revision": revision},
    )
    if not started:
        return run
    opportunity = session.get(Opportunity, opportunity_id)
    if opportunity is None:
        return finish_run(run, "failed", error="opportunity not found", outputs={"analysis_complete": False, "state": "incomplete"})
    try:
        from govcon.enrich.attachment_refs import attachment_refs_for
        from govcon.enrich.attachments import download_attachments

        refs = attachment_refs_for(opportunity)
        unread: list[str] = []
        if refs:
            files = download_attachments(session, opportunity, settings=settings)
            for stored in files:
                if stored.extraction_status in {"error", "download_failed"} or stored.downloaded_at is None:
                    unread.append(stored.url or stored.filename or "attachment")
        requirements = _requirements_from_listing(opportunity)
        _store_requirements(session, opportunity.id, requirements)
        state = "incomplete" if unread else "extracted"
        status = "failed" if unread and not (opportunity.description or "").strip() else (
            "completed_with_errors" if unread else "succeeded"
        )
        finish_run(run, status, outputs={
            "requirements": requirements,
            "unread_attachments": unread,
            "attachment_count": len(refs),
            "analysis_complete": not unread,
            "state": state,
        }, error=("attachments were not read: " + ", ".join(unread[:5])) if unread else None)
    except Exception as exc:
        finish_run(run, "failed", error=redact(str(exc)), outputs={"analysis_complete": False, "state": "incomplete"})
    return run


def execute_matching(
    session: Session, settings: Settings, *, opportunity_id: int, trigger: str, parent_run_id: int | None,
) -> BotRun:
    del settings
    revision = _revision(session, opportunity_id)
    run, started = begin_run(
        session, bot_name="matching", idempotency_key=f"matching:{opportunity_id}:{revision}",
        trigger=trigger, opportunity_id=opportunity_id, source_revision=revision,
        parent_run_id=parent_run_id, inputs={"source_revision": revision},
    )
    if not started:
        return run
    opportunity = session.get(Opportunity, opportunity_id)
    if opportunity is None:
        return finish_run(run, "failed", error="opportunity not found", outputs={"matched": False, "state": "incomplete"})
    try:
        from govcon.matching.eligibility import pursuit_eligibility
        from govcon.matching.engine import evaluate_match
        from govcon.models import Match, Watchlist

        eligibility, reason = pursuit_eligibility(opportunity)
        groups = []
        matched = False
        for watchlist in session.scalars(select(Watchlist).where(Watchlist.enabled.is_(True))).all():
            is_match, matched_on, score = evaluate_match(watchlist, opportunity)
            matched = matched or is_match
            groups.append({
                "watchlist_id": watchlist.id,
                "watchlist": watchlist.name,
                "matched": is_match,
                "score": str(score),
                "active_groups": matched_on.get("active_groups"),
                "passing_groups": matched_on.get("passing_groups"),
                "failed_groups": [
                    name for name, evidence in (matched_on.get("groups") or {}).items()
                    if isinstance(evidence, dict) and evidence.get("status") == "fail"
                ],
            })
        rank = session.scalar(
            select(Match.rank_score).where(Match.opportunity_id == opportunity_id, Match.active.is_(True)).limit(1)
        )
        hard = []
        if eligibility == "ineligible":
            hard.append(reason or "ineligible")
        finish_run(run, "succeeded", outputs={
            "matched": matched,
            "eligibility": eligibility,
            "eligibility_reason": reason,
            "hard_disqualifiers": hard,
            "watchlists": groups,
            "rank_score": float(rank) if rank is not None else None,
            "and_rule": "Every watchlist group that has values must pass. An empty group is ignored. Values inside one group are combined with OR.",
            "state": "matched" if matched else "not_a_match",
            "analysis_complete": True,
        })
    except Exception as exc:
        finish_run(run, "failed", error=redact(str(exc)), outputs={"matched": False, "state": "incomplete", "analysis_complete": False})
    return run


def execute_compliance(
    session: Session, settings: Settings, *, opportunity_id: int, trigger: str, parent_run_id: int | None,
) -> BotRun:
    del settings
    revision = _revision(session, opportunity_id)
    run, started = begin_run(
        session, bot_name="compliance", idempotency_key=f"compliance:{opportunity_id}:{revision}",
        trigger=trigger, opportunity_id=opportunity_id, source_revision=revision,
        parent_run_id=parent_run_id, inputs={"source_revision": revision},
    )
    if not started:
        return run
    opportunity = session.get(Opportunity, opportunity_id)
    if opportunity is None:
        return finish_run(run, "failed", error="opportunity not found", outputs={"state": "incomplete", "analysis_complete": False})
    try:
        from govcon.matching.eligibility import pursuit_eligibility

        eligibility, reason = pursuit_eligibility(opportunity)
        questions = []
        if opportunity.response_deadline is None:
            questions.append("Response deadline is not on the listing.")
        if not opportunity.set_aside_code:
            questions.append("Set-aside is not on the listing.")
        if not opportunity.naics_code:
            questions.append("NAICS is not on the listing.")
        if not (opportunity.description or "").strip():
            questions.append("No description text is stored, so clauses were not read.")
        clauses = _cited_clauses(opportunity.description or "")
        if eligibility == "unknown":
            questions.append(reason or "Eligibility is unknown.")
        finish_run(run, "completed_with_errors" if questions else "succeeded", outputs={
            "eligibility": eligibility,
            "eligibility_reason": reason,
            "unanswered": questions,
            "clauses": clauses,
            "set_aside": opportunity.set_aside_code or None,
            "state": "questions_open" if questions else "checked",
            "analysis_complete": not questions,
        })
        if questions:
            open_approval(session, run, kind="compliance_questions", opportunity_id=opportunity_id,
                          summary="Compliance questions need a person.", evidence={"unanswered": questions})
            run.status = "waiting_approval"
    except Exception as exc:
        finish_run(run, "failed", error=redact(str(exc)), outputs={"state": "incomplete", "analysis_complete": False})
    return run


def execute_amendment(
    session: Session, settings: Settings, *, opportunity_id: int, trigger: str, parent_run_id: int | None,
) -> BotRun:
    del settings
    revision = _revision(session, opportunity_id)
    run, started = begin_run(
        session, bot_name="amendment", idempotency_key=f"amendment:{opportunity_id}:{revision}",
        trigger=trigger, opportunity_id=opportunity_id, source_revision=revision,
        parent_run_id=parent_run_id, inputs={"source_revision": revision},
    )
    if not started:
        return run
    try:
        events = session.scalars(
            select(OpportunityEvent)
            .where(OpportunityEvent.opportunity_id == opportunity_id)
            .order_by(OpportunityEvent.detected_at.desc())
            .limit(20)
        ).all()
        if not events:
            return finish_run(run, "skipped", outputs={
                "changes": [], "state": "no_events", "note": "No source events are stored. That is not a statement that nothing changed outside this database.",
                "analysis_complete": True,
            })
        changes = [{
            "event_type": event.event_type,
            "detected_at": _aware(event.detected_at).isoformat(),
            "impact": _IMPACT.get(event.event_type, "The source record changed. The effect on price and compliance is unknown until a person checks the notice."),
        } for event in events]
        finish_run(run, "succeeded", outputs={"changes": changes, "state": "explained", "analysis_complete": True})
    except Exception as exc:
        finish_run(run, "failed", error=redact(str(exc)), outputs={"state": "incomplete", "analysis_complete": False})
    return run


def execute_awards(
    session: Session, settings: Settings, *, opportunity_id: int, trigger: str, parent_run_id: int | None,
) -> BotRun:
    del settings
    revision = _revision(session, opportunity_id)
    run, started = begin_run(
        session, bot_name="awards", idempotency_key=f"awards:{opportunity_id}:{revision}",
        trigger=trigger, opportunity_id=opportunity_id, source_revision=revision,
        parent_run_id=parent_run_id, inputs={"source_revision": revision},
    )
    if not started:
        return run
    opportunity = session.get(Opportunity, opportunity_id)
    if opportunity is None:
        return finish_run(run, "failed", error="opportunity not found", outputs={"state": "incomplete"})
    try:
        from govcon.matching.pricing import recent_award_comps

        points = recent_award_comps(session, nsn=opportunity.nsn, psc_code=opportunity.psc_code, limit=5)
        comps = [{
            "award_id": point.award_id,
            "action_date": point.action_date.isoformat() if point.action_date else None,
            "amount": str(point.amount) if point.amount is not None else None,
            "agency": point.awarding_agency,
            "description": point.description,
        } for point in points]
        finish_run(run, "succeeded" if comps else "completed_with_errors", outputs={
            "comps": comps,
            "disclaimer": "These are historical USAspending amounts. They are not the value of this opportunity.",
            "state": "history" if comps else "unknown",
            "analysis_complete": True,
        })
    except Exception as exc:
        finish_run(run, "failed", error=redact(str(exc)), outputs={"state": "incomplete", "analysis_complete": False, "comps": []})
    return run


def execute_bid(
    session: Session, settings: Settings, *, opportunity_id: int, trigger: str, parent_run_id: int | None,
) -> BotRun:
    revision = _revision(session, opportunity_id)
    run, started = begin_run(
        session, bot_name="bid_decision", idempotency_key=f"bid_decision:{opportunity_id}:{revision}",
        trigger=trigger, opportunity_id=opportunity_id, source_revision=revision,
        parent_run_id=parent_run_id, inputs={"source_revision": revision},
    )
    if not started:
        return run
    try:
        from govcon.decision.engine import run_preliminary_decision_package

        package = run_preliminary_decision_package(
            session, opportunity_id=opportunity_id, settings=settings, allow_llm_fallback=False,
        )
        providers = sorted({item.provider for item in package.bundle_runs})
        recommendation = package.bid_decision.recommendation
        missing = (package.bid_decision.missing_information or {}).get("items") or []
        finish_run(run, "waiting_approval", outputs={
            "recommendation": recommendation,
            "providers": providers,
            "missing_information": missing,
            "note": "JEV is used only when the classification gateway allows the package. Otherwise the local rules engine is the recommendation.",
            "state": "needs_decision",
            "analysis_complete": False,
        })
        open_approval(
            session, run, kind="bid_recommendation", opportunity_id=opportunity_id,
            summary=f"Rules/JEV recommendation: {recommendation}. This does not submit a bid.",
            evidence={"recommendation": recommendation, "providers": providers, "missing_information": missing},
        )
    except Exception as exc:
        finish_run(run, "failed", error=redact(str(exc)), outputs={"state": "incomplete", "analysis_complete": False})
    return run


def execute_alert(
    session: Session, settings: Settings, *, slot: str, trigger: str, parent_run_id: int | None,
) -> BotRun:
    run, started = begin_run(
        session, bot_name="alert", idempotency_key=f"alert:{slot}",
        trigger=trigger, parent_run_id=parent_run_id, inputs={"slot": slot},
    )
    if not started:
        return run
    try:
        from govcon.alerts.digest import run_digest

        digest = run_digest(session, settings=settings, external_delivery=False)
        path = _write_cycle_summary(settings, slot, session, parent_run_id, digest_path=digest.path)
        finish_run(run, "waiting_approval", outputs={
            "channel": "outbox",
            "digest_path": digest.path,
            "summary_path": path,
            "new_count": digest.new_count,
            "amendment_count": digest.amendment_count,
            "external_send": False,
            "state": "needs_decision",
            "analysis_complete": False,
        })
        open_approval(
            session, run, kind="daily_summary",
            summary="Daily summary is in the outbox. Approving it does not send email.",
            evidence={"path": path, "new_count": digest.new_count},
        )
    except Exception as exc:
        finish_run(run, "failed", error=redact(str(exc)), outputs={"external_send": False, "state": "incomplete"})
    return run


def execute_operations(
    session: Session, settings: Settings, *, slot: str, trigger: str, parent_run_id: int | None,
) -> BotRun:
    run, started = begin_run(
        session, bot_name="operations", idempotency_key=f"operations:{slot}",
        trigger=trigger, parent_run_id=parent_run_id, inputs={"slot": slot},
    )
    if not started:
        return run
    try:
        from govcon.ops.health import collect_health

        report = collect_health(session, settings)
        down = [item["name"] for item in report["checks"] if item["status"] in {"down", "degraded", "stale"}]
        finish_run(run, "completed_with_errors" if down else "succeeded", outputs={
            "checks": report["checks"],
            "needs_attention": down,
            "state": "degraded" if down else "healthy",
            "analysis_complete": True,
        })
    except Exception as exc:
        finish_run(run, "failed", error=redact(str(exc)), outputs={"state": "incomplete", "analysis_complete": False})
    return run


def _requirements_from_listing(opportunity: Opportunity) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    fields = (
        ("response_deadline", "Response deadline"),
        ("naics_code", "NAICS"),
        ("psc_code", "PSC"),
        ("set_aside_code", "Set-aside"),
        ("place_of_performance", "Place of performance"),
    )
    for field, label in fields:
        value = getattr(opportunity, field, None)
        formatter = getattr(value, "isoformat", None)
        shown = formatter() if callable(formatter) else value
        rows.append({
            "text": f"{label}: {shown}" if shown else f"{label}: unknown",
            "status": "stated" if shown else "unknown",
            "mandatory": None,
            "citation": {"source": "opportunity listing", "field": field, "quote": str(shown) if shown else None},
        })
    for line in (opportunity.description or "").splitlines():
        cleaned = line.strip()
        if not cleaned or not _SHALL.search(cleaned):
            continue
        rows.append({
            "text": cleaned[:500],
            "status": "extracted",
            "mandatory": bool(re.search(r"\b(shall|must)\b", cleaned, re.IGNORECASE)),
            "citation": {"source": "description", "field": "description", "quote": cleaned[:300]},
        })
        if len(rows) >= 40:
            break
    return rows


def _store_requirements(session: Session, opportunity_id: int, rows: list[dict[str, Any]]) -> None:
    for item in rows:
        if item["status"] != "extracted":
            continue
        digest = hashlib.sha256(item["text"].encode("utf-8")).hexdigest()
        existing = session.scalar(
            select(Requirement).where(
                Requirement.opportunity_id == opportunity_id,
                Requirement.source_text_hash == digest,
            )
        )
        if existing is not None:
            continue
        session.add(Requirement(
            opportunity_id=opportunity_id,
            requirement_text=item["text"],
            mandatory=item["mandatory"],
            source_section="description",
            source_quote=(item["citation"] or {}).get("quote"),
            source_text_hash=digest,
            extraction_pass="document_bot",
            status="unreviewed",
            created_by="document_bot",
            source_refs=[item["citation"]],
        ))


def _cited_clauses(description: str) -> list[dict[str, str]]:
    found = []
    for line in description.splitlines():
        if re.search(r"\b(FAR|DFARS)\b", line):
            found.append({"quote": line.strip()[:300], "source": "description"})
        if len(found) >= 10:
            break
    return found


def _ids_since(session: Session, started: datetime) -> list[int]:
    rows = session.scalars(
        select(OpportunityEvent.opportunity_id)
        .where(OpportunityEvent.detected_at >= started)
        .distinct()
    ).all()
    return [int(item) for item in rows]


def _observed_ingest(session: Session) -> tuple[dict[str, Any], list[int]]:
    sam = _latest_job(session, ("sched:sam_ingest", "sam_opportunities"))
    dibbs = _latest_job(session, ("sched:dibbs_ingest", "dibbs_index"))
    stamps = [item.started_at for item in (sam, dibbs) if item is not None and item.started_at is not None]
    ids = _ids_since(session, min(stamps)) if stamps else []
    return {"sam": _run_public(sam), "dibbs": _run_public(dibbs)}, ids


def _latest_job(session: Session, jobs: tuple[str, ...]) -> IngestionRun | None:
    return session.scalar(
        select(IngestionRun).where(IngestionRun.job.in_(jobs)).order_by(IngestionRun.started_at.desc()).limit(1)
    )


def _run_public(run: IngestionRun | None) -> dict[str, Any]:
    if run is None:
        return {"status": "skipped", "error": "no local ingest run is recorded", "fetched": 0, "inserted": 0, "updated": 0}
    return {
        "status": run.status or "unknown",
        "error": None,
        "fetched": run.fetched or 0,
        "inserted": run.inserted or 0,
        "updated": run.updated or 0,
    }


def _step_public(result: Any) -> dict[str, Any]:
    return {
        "status": result.status,
        "error": result.error,
        "fetched": result.fetched,
        "inserted": result.inserted,
        "updated": result.updated,
    }


def _revision(session: Session, opportunity_id: int) -> str:
    from govcon.workflow.source_revision import current_source_revision

    return str(current_source_revision(session, opportunity_id))


def _write_cycle_summary(settings: Settings, slot: str, session: Session, parent_run_id: int | None, *, digest_path: str | None) -> str:
    from pathlib import Path

    from govcon.alerts.digest import resolve_outbox

    children: list[BotRun] = []
    if parent_run_id is not None:
        children = list(session.scalars(select(BotRun).where(BotRun.parent_run_id == parent_run_id)).all())
    lines = [
        "<!DOCTYPE html><html lang=en><meta charset=utf-8><title>GovCon daily summary</title><body>",
        f"<h1>GovCon cycle {slot}</h1>",
        "<p>This file is the outbox copy. Approving it in GovCon does not send email.</p>",
        f"<p>Match digest: {digest_path or 'none'}</p><ul>",
    ]
    for child in children:
        state = (child.outputs or {}).get("state")
        lines.append(f"<li>{child.bot_name} #{child.id}: {child.status} ({state or 'no state'})</li>")
    lines.append("</ul></body></html>")
    folder = resolve_outbox(settings)
    folder.mkdir(parents=True, exist_ok=True)
    path = Path(folder) / f"bot-summary-{slot.replace(':', '')}.html"
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value
