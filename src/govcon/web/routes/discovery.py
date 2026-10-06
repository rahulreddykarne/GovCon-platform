"""Discovery endpoints and supporting views."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import desc, func, select, text

from govcon.audit import record_audit
from govcon.collaboration.users import PermissionDenied
from govcon.db import session_scope
from govcon.models import (
    Award,
    Match,
    Opportunity,
    Pursuit,
    ReviewSession,
    Vendor,
    Watchlist,
)
from govcon.web.helpers import deadline_info, format_value, match_summary
from govcon.web.presentation import ViewRow
from govcon.web.routes.common import (
    _WORKFLOW_ERRORS,
    _actor,
    _error_text,
    _NeedsLogin,
    _render,
    _require_login,
)
from govcon.workflow.invalidation import lock_opportunity
from govcon.workflow.transitions import TERMINAL_PURSUIT_STAGES


def inbox(request: Request, page: Annotated[int, Query(ge=1, le=1_000_000)] = 1) -> Response:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)

    from govcon.matching.auto_pursue import needs_eligibility_decision
    from govcon.workflow.app_settings import AUTO_PURSUE, get_setting

    now = datetime.now(UTC)
    with session_scope() as db:
        policy = get_setting(db, AUTO_PURSUE)
        ranked = select(
            Match.id.label("match_id"),
            func.row_number().over(partition_by=Match.opportunity_id,
                order_by=(Match.rank_score.desc().nullslast(), Match.id)).label("position"),
        ).join(Watchlist, Watchlist.id == Match.watchlist_id).where(
            Watchlist.enabled.is_(True), Match.status == "new", Match.active.is_(True),
            ~select(Pursuit.id).where(Pursuit.opportunity_id == Match.opportunity_id).exists(),
        ).subquery()
        chosen = select(ranked.c.match_id).where(ranked.c.position == 1)
        total = db.scalar(select(func.count()).select_from(chosen.subquery())) or 0
        page_size = 100
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, pages)
        rows = db.execute(select(Match, Opportunity).join(Opportunity, Match.opportunity_id == Opportunity.id)
            .where(Match.id.in_(chosen)).order_by(Match.rank_score.desc().nullslast(),
                Opportunity.response_deadline.asc().nullslast(), Opportunity.id)
            .offset((page - 1) * page_size).limit(page_size)).all()
        names: dict[int, list[str]] = {}
        for opp_id, name in db.execute(select(Match.opportunity_id, Watchlist.name)
            .join(Watchlist, Match.watchlist_id == Watchlist.id).where(
                Match.opportunity_id.in_([opp.id for _, opp in rows]), Match.active.is_(True),
                Watchlist.enabled.is_(True)).order_by(Watchlist.name)):
            names.setdefault(opp_id, []).append(name)
        matches = []
        for m, opp in rows:
            deadline_label, deadline_class = deadline_info(opp.response_deadline)
            matches.append(
                {
                    "match_id": m.id,
                    "opp_id": opp.id,
                    "title": opp.title,
                    "source": opp.source,
                    "psc": opp.psc_code,
                    "agency": opp.agency_path,
                    "deadline_label": deadline_label,
                    "deadline_class": deadline_class,
                    "estimated_value": format_value(opp.estimated_value_min, opp.estimated_value_max),
                    "matched_on": match_summary(m.matched_on),
                    "watchlists": names.get(opp.id, []),
                    "alerted": m.alerted_at is not None,
                    "rank_score": float(m.rank_score) if m.rank_score is not None else None,
                    "rank_factors": m.rank_factors or [],
                    "needs_eligibility_decision": needs_eligibility_decision(m, opp, policy, now),
                }
            )
        groups = [{"watchlist_name": "New opportunities", "matches": matches}] if matches else []
        from govcon.models import BotApproval, BotRun
        from govcon.ops.health import collect_health

        pending_approvals = db.scalar(
            select(func.count()).select_from(BotApproval).where(BotApproval.status == "pending")
        ) or 0
        latest_orchestrator = db.scalar(
            select(BotRun).where(BotRun.bot_name == "orchestrator").order_by(desc(BotRun.started_at)).limit(1)
        )
        orchestrator_view = None
        if latest_orchestrator is not None:
            orchestrator_view = {
                "status": latest_orchestrator.status,
                "state": (latest_orchestrator.outputs or {}).get("state") or latest_orchestrator.status,
            }
        try:
            health = collect_health(db, request.app.state.settings)
            unhealthy = [item["name"] for item in health["checks"] if item["status"] in {"down", "degraded", "stale"}]
        except Exception:
            unhealthy = ["health"]
        scored: list[tuple[float, dict[str, Any]]] = []
        for item in matches:
            score = item["rank_score"]
            if isinstance(score, (int, float)) and not isinstance(score, bool):
                scored.append((float(score), item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        promising = [item for _, item in scored[:3]]
    return _render(request, "inbox.html", {
        "groups": groups, "total_count": total, "page": page, "pages": pages, "active_page": "inbox",
        "pending_approvals": pending_approvals, "orchestrator": orchestrator_view,
        "unhealthy": unhealthy[:6], "promising": promising,
    }, user)


def inbox_action(
    request: Request,
    match_id: Annotated[int, Form()],
    action: Annotated[str, Form()],
) -> Response:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)

    from html import escape

    def failure(message: str, status: int) -> Response:
        return HTMLResponse(f'<div class="alert alert-error" role="alert">{escape(message)}</div>', status_code=status)

    if action not in ("seen", "dismissed", "reviewing", "pursuing"):
        return failure("Unknown Inbox action.", 400)
    try:
        with session_scope() as db:
            actor = _actor(db, user, "review")
            selected = db.get(Match, match_id)
            if selected is None:
                return failure("The match no longer exists. Reload the Inbox.", 404)
            opp_id = selected.opportunity_id
            lock_opportunity(db, opp_id)
            if action == "pursuing":
                from govcon.workflow.pursuits import create_or_get_pursuit

                create_or_get_pursuit(db, opportunity_id=opp_id, actor=actor, origin="web_inbox")
            for match in db.scalars(select(Match).where(Match.opportunity_id == opp_id)):
                old = match.status
                match.status = action
                record_audit(db, action_type="match_status_changed", user_id=actor.id,
                    opportunity_id=opp_id, entity_type="matches", entity_id=match.id,
                    old_value={"status": old}, new_value={"status": action})
    except PermissionDenied:
        return failure("Your role cannot change Inbox matches.", 403)
    except _WORKFLOW_ERRORS as exc:
        return failure(_error_text(exc), 409)
    target = f"/workspace/{opp_id}" if action == "pursuing" else "/"
    if request.headers.get("HX-Request") == "true":
        if action == "pursuing":
            return HTMLResponse("", headers={"HX-Redirect": target})
        return HTMLResponse("")
    return RedirectResponse(target, status_code=303)


def search(
    request: Request,
    q: str | None = None,
    source: str | None = None,
    psc: str | None = None,
    naics: str | None = None,
    status_filter: str | None = Query(None, alias="status"),
    page: Annotated[int, Query(ge=1, le=1_000_000)] = 1,
) -> Response:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)

    LIMIT = 50
    results = None
    has_more = False

    if q or source or psc or naics or status_filter:
        with session_scope() as db:
            stmt = select(Opportunity)
            if q:
                stmt = stmt.where(
                    text("to_tsvector('english', coalesce(title,'') || ' ' || coalesce(description,'')) @@ plainto_tsquery('english', :q)")
                ).params(q=q)
            if source:
                stmt = stmt.where(Opportunity.source == source)
            if psc:
                stmt = stmt.where(Opportunity.psc_code.like(f"{psc}%"))
            if naics:
                stmt = stmt.where(Opportunity.naics_code.like(f"{naics}%"))
            if status_filter:
                stmt = stmt.where(Opportunity.status == status_filter)
            if q:
                stmt = stmt.order_by(text("ts_rank_cd(to_tsvector('english', coalesce(title,'') || ' ' || coalesce(description,'')), plainto_tsquery('english', :q)) DESC"))
            stmt = stmt.order_by(Opportunity.response_deadline.asc().nullslast(), Opportunity.id).offset((page - 1) * LIMIT).limit(LIMIT + 1)
            rows = list(db.scalars(stmt).all())
            has_more = len(rows) > LIMIT
            rows = rows[:LIMIT]
            results = []
            for r in rows:
                dl, dlc = deadline_info(r.response_deadline)
                results.append(
                    ViewRow({
                        "id": r.id,
                        "title": r.title,
                        "source": r.source,
                        "source_id": r.source_id,
                        "solicitation_number": r.solicitation_number,
                        "psc_code": r.psc_code,
                        "agency_path": r.agency_path,
                        "value_display": format_value(r.estimated_value_min, r.estimated_value_max),
                        "deadline_display": dl,
                        "deadline_class": dlc,
                        "status": r.status,
                    })
                )

    return _render(request, "search.html", {
        "results": results,
        "query": q,
        "source": source,
        "psc": psc,
        "naics": naics,
        "status": status_filter,
        "limit": LIMIT,
        "page": page,
        "has_more": has_more,
        "active_page": "search",
    }, user)


_PIPELINE_COLUMNS = [
    ("matched", "Matched"), ("preparing", "Preparing"), ("in_review", "In review"),
    ("drafting", "Approved / drafting"), ("ready", "Ready / submitted"), ("closed", "Closed"),
]


def pipeline(request: Request, matched_page: Annotated[int, Query(ge=1, le=1_000_000)] = 1) -> Response:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    closed_filter = request.query_params.get("closed", "all")
    if closed_filter not in {"all", "won", "lost", "no_bid", "cancelled"}:
        return HTMLResponse("Unknown closed outcome filter", status_code=422)
    with session_scope() as db:
        pursuits = db.execute(select(Pursuit, Opportunity, ReviewSession)
            .join(Opportunity, Pursuit.opportunity_id == Opportunity.id)
            .outerjoin(ReviewSession, ReviewSession.opportunity_id == Opportunity.id)
            .order_by(Opportunity.response_deadline.asc().nullslast(), Opportunity.id)).all()
        matched = select(Opportunity).where(
            ~select(Pursuit.id).where(Pursuit.opportunity_id == Opportunity.id).exists(),
            select(Match.id).join(Watchlist, Match.watchlist_id == Watchlist.id).where(
                Match.opportunity_id == Opportunity.id, Match.active.is_(True),
                Match.status.in_(["new", "seen", "reviewing"]), Watchlist.enabled.is_(True)).exists(),
        )
        matched_count = db.scalar(select(func.count()).select_from(matched.subquery())) or 0
        matched_pages = max(1, (matched_count + 99) // 100)
        matched_page = min(matched_page, matched_pages)
        new_matches = db.scalars(matched.order_by(Opportunity.response_deadline.asc().nullslast(), Opportunity.id)
            .offset((matched_page - 1) * 100).limit(100)).all()
        cards: dict[str, list[dict[str, Any]]] = {key: [] for key, _ in _PIPELINE_COLUMNS}
        active_count = matched_count
        closed_count = 0

        def card(opp: Opportunity, stage: str, margin: Any = None) -> dict[str, Any]:
            dl, dlc = deadline_info(opp.response_deadline)
            return {"opp_id": opp.id, "title": opp.title, "source_id": opp.source_id,
                    "deadline_label": dl, "deadline_class": dlc, "stage": stage,
                    "stage_label": stage.replace("_", " "),
                    "value": format_value(opp.estimated_value_min, opp.estimated_value_max), "margin": margin}

        for pursuit, opp, review in pursuits:
            stage = pursuit.stage or "evaluating"
            if stage in TERMINAL_PURSUIT_STAGES:
                closed_count += 1
                if closed_filter == "all" or stage == closed_filter:
                    cards["closed"].append(card(opp, stage, pursuit.margin_pct))
                continue
            active_count += 1
            if stage in {"ready_to_submit", "submitted"}:
                column = "ready"
            elif stage in {"bid_approved", "drafting", "review"}:
                column = "drafting"
            elif review and review.status in {"ready_for_review", "under_review", "review_complete", "approval_pending", "returned_for_review"}:
                column = "in_review"
            else:
                column = "preparing"
            cards[column].append(card(opp, stage, pursuit.margin_pct))
        cards["matched"] = [card(opp, "matched") for opp in new_matches]
        columns = [{"key": key, "label": label, "cards": cards[key]} for key, label in _PIPELINE_COLUMNS]
    return _render(request, "pipeline.html", {"columns": columns, "total_count": active_count,
        "closed_count": closed_count, "closed_filter": closed_filter, "matched_count": matched_count,
        "matched_page": matched_page, "matched_pages": matched_pages, "active_page": "pipeline"}, user)


def vendors(request: Request) -> Response:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)

    q = request.query_params.get("q", "").strip()
    vendor = None
    vendor_awards = None
    results = None

    if q:
        with session_scope() as db:
            # Try exact UEI match first
            vendor = db.scalar(select(Vendor).where(Vendor.uei == q))
            if vendor is None:
                # Try CAGE
                vendor = db.scalar(select(Vendor).where(Vendor.cage_code == q))
            if vendor is None:
                # Try name search
                results = db.scalars(
                    select(Vendor).where(Vendor.legal_name.ilike(f"%{q}%")).limit(20)
                ).all()
            else:
                vendor_awards = db.scalars(
                    select(Award).where(Award.recipient_uei == vendor.uei)
                    .order_by(desc(Award.action_date)).limit(30)
                ).all()

    return _render(request, "vendors.html", {
        "query": q or None,
        "vendor": vendor,
        "vendor_awards": vendor_awards,
        "results": results,
        "active_page": "vendors",
    }, user)
