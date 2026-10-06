"""/bots: run history, evidence, and the approvals queue."""

from __future__ import annotations

from typing import Annotated

from fastapi import Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import desc, select

from govcon.bots.catalog import CATALOG
from govcon.bots.orchestrator import execute_orchestrator, new_slot
from govcon.bots.store import decide_approval
from govcon.db import session_scope
from govcon.models import BotApproval, BotRun
from govcon.web.routes import _NeedsLogin, _redirect, _render, _require_login


def bots_page(request: Request) -> Response:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    with session_scope() as db:
        runs = db.scalars(select(BotRun).order_by(desc(BotRun.started_at), desc(BotRun.id)).limit(80)).all()
        approvals = db.scalars(
            select(BotApproval).where(BotApproval.status == "pending").order_by(desc(BotApproval.requested_at))
        ).all()
        latest: dict[str, BotRun] = {}
        for run in runs:
            latest.setdefault(run.bot_name, run)
        cards = []
        for name, spec in CATALOG.items():
            run = latest.get(name)
            cards.append({
                "name": name,
                "spec": spec,
                "run": run,
                "why": _why(run),
                "evidence": _evidence(run),
            })
        pending = [_approval_view(row) for row in approvals]
    return _render(request, "bots.html", {
        "cards": cards,
        "pending": pending,
        "can_decide": user.role in {"owner", "approver"},
        "active_page": "bots",
    }, user)


def bots_run(
    request: Request,
    bot_name: str,
    pull: Annotated[str | None, Form()] = None,
) -> Response:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    if user.role not in {"owner", "approver"}:
        return HTMLResponse("Not permitted", status_code=403)
    if bot_name != "orchestrator":
        return _redirect("/bots", notice="Run the orchestrator. It starts the other bots.")
    settings = request.app.state.settings
    with session_scope() as db:
        execute_orchestrator(
            db, settings, trigger=f"manual:{user.id}", slot=new_slot(), pull=pull == "yes",
        )
    return _redirect("/bots", notice="Orchestrator finished. Nothing was emailed or submitted.")


def bots_decide(
    request: Request,
    approval_id: int,
    action: str,
    note: Annotated[str | None, Form()] = None,
) -> Response:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    if user.role not in {"owner", "approver"}:
        return HTMLResponse("Not permitted", status_code=403)
    if action not in {"approved", "rejected"}:
        return HTMLResponse("Unknown action", status_code=404)
    with session_scope() as db:
        decide_approval(db, approval_id, user_id=user.id, status=action, note=note)
    return _redirect("/bots", notice="Decision recorded. No email was sent and AI sharing was not changed.")


def _why(run: BotRun | None) -> str:
    if run is None:
        return "This bot has not run."
    outputs = run.outputs or {}
    if run.error:
        return run.error
    if outputs.get("and_rule") and outputs.get("matched") is not None:
        fit = "matched a watchlist" if outputs.get("matched") else "did not match a watchlist"
        return f"It {fit}. {outputs['and_rule']}"
    if outputs.get("recommendation"):
        providers = ", ".join(outputs.get("providers") or []) or "rules"
        return f"Recommendation {outputs['recommendation']} from {providers}."
    if outputs.get("disclaimer"):
        count = len(outputs.get("comps") or [])
        return f"{count} historical award(s). {outputs['disclaimer']}"
    if outputs.get("unanswered"):
        return "Open questions: " + "; ".join(outputs["unanswered"][:4])
    if outputs.get("state"):
        return f"State: {outputs['state']}."
    return run.status


def _evidence(run: BotRun | None) -> list[str]:
    if run is None or not run.outputs:
        return []
    outputs = run.outputs
    lines: list[str] = []
    for item in (outputs.get("requirements") or [])[:6]:
        cite = item.get("citation") or {}
        lines.append(f"{item.get('text')} (source: {cite.get('source')} {cite.get('field') or ''})".strip())
    for item in (outputs.get("changes") or [])[:6]:
        lines.append(f"{item.get('event_type')}: {item.get('impact')}")
    for item in (outputs.get("comps") or [])[:4]:
        lines.append(f"Award {item.get('award_id')} amount {item.get('amount') or 'unknown'} on {item.get('action_date') or 'unknown date'}")
    for item in (outputs.get("unanswered") or [])[:6]:
        lines.append(item)
    if outputs.get("summary_path"):
        lines.append(f"Outbox file: {outputs['summary_path']}")
    if outputs.get("external_send") is False:
        lines.append("External send: no")
    return lines


def _approval_view(row: BotApproval) -> dict[str, object]:
    return {
        "id": row.id,
        "kind": row.kind,
        "summary": row.summary,
        "opportunity_id": row.opportunity_id,
        "evidence": row.evidence or {},
    }
