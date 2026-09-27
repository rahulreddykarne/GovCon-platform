"""Phase 14 FastAPI routes — all product pages."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from fastapi import Cookie, Form, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select, text, or_
from sqlalchemy.orm import Session as OrmSession

from govcon.collaboration.comments import list_comments
from govcon.collaboration.users import (
    AuthError,
    PermissionDenied,
    authenticate,
    create_session,
    invite_user,
    logout,
    require_permission,
    user_for_token,
)
from govcon.db import session_scope
from govcon.models import (
    AIAnalysis,
    AuditEvent,
    Award,
    BidDecision,
    ComplianceRun,
    Contact,
    IngestionRun,
    Match,
    Notification,
    Opportunity,
    OpportunityEvent,
    OpportunitySnapshot,
    Proposal,
    ProposalVersion,
    Pursuit,
    Requirement,
    ReviewAssignment,
    ReviewComment,
    ReviewSession,
    SchedulerJobRun,
    StoredFile,
    Submission,
    User,
    UserSession,
    Vendor,
    Watchlist,
)
from govcon.learning.analytics import WIN_PROFILE_MINIMUM, outcome_analytics, similar_past_outcomes
from govcon.learning.outcomes import NO_BID_CATEGORIES, record_outcome
from govcon.web.helpers import deadline_info, format_value

import pathlib

_TEMPLATE_DIR = pathlib.Path(__file__).parent.parent / "templates"
_templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

# ── Deadline helper exposed to templates ──────────────────────────────────────


def _add_template_globals(templates: Jinja2Templates) -> None:
    templates.env.globals["now"] = lambda: datetime.now(UTC)


_add_template_globals(_templates)


# ── Cookie / auth helpers ──────────────────────────────────────────────────────

_COOKIE_NAME = "govcon_session"
_COOKIE_MAX_AGE = 12 * 3600  # matches session_ttl_hours default


def _current_user(request: Request) -> User | None:
    raw = request.cookies.get(_COOKIE_NAME)
    if not raw:
        return None
    with session_scope() as db:
        return user_for_token(db, raw)


def _require_login(request: Request) -> User:
    user = _current_user(request)
    if user is None:
        raise _NeedsLogin()
    return user


def _unread_count(user: User) -> int:
    with session_scope() as db:
        count = db.scalar(
            select(func.count()).select_from(Notification).where(
                Notification.user_id == user.id,
                Notification.read_at.is_(None),
            )
        )
        return count or 0


class _NeedsLogin(Exception):
    pass


def _render(request: Request, template: str, ctx: dict[str, Any], user: User) -> HTMLResponse:
    ctx["current_user"] = user
    ctx["unread_notification_count"] = _unread_count(user)
    return _templates.TemplateResponse(request, template, ctx)


def _opportunity_external_links(links: dict | None) -> list[tuple[str, str]]:
    """Flatten SAM's URL strings and link arrays for the detail page."""
    if not isinstance(links, dict):
        return []
    result: list[tuple[str, str]] = []
    for label, value in links.items():
        items = value if isinstance(value, list) else [value]
        for item in items:
            url = item.get("href") if isinstance(item, dict) else item
            if isinstance(url, str) and url.startswith(("https://", "http://")):
                result.append((str(label), url))
    return result


# ── Login / logout ────────────────────────────────────────────────────────────


def login_get(request: Request) -> HTMLResponse:
    if _current_user(request):
        return RedirectResponse("/", status_code=303)
    return _templates.TemplateResponse(request, "login.html", {"error": None})


def login_post(
    request: Request,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> HTMLResponse:
    with session_scope() as db:
        user = authenticate(db, email, password)
        if user is None:
            return _templates.TemplateResponse(
                request,
                "login.html",
                {"error": "Invalid email or password.", "prefill_email": email},
                status_code=401,
            )
        raw_token = create_session(db, user)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        _COOKIE_NAME,
        raw_token,
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return resp


def logout_post(request: Request) -> HTMLResponse:
    raw = request.cookies.get(_COOKIE_NAME)
    if raw:
        with session_scope() as db:
            logout(db, raw)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(_COOKIE_NAME)
    return resp


# ── Inbox ─────────────────────────────────────────────────────────────────────


def inbox(request: Request) -> HTMLResponse:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)

    groups: list[dict] = []
    total = 0

    with session_scope() as db:
        wls = db.scalars(select(Watchlist).where(Watchlist.enabled == True)).all()
        for wl in wls:
            rows = db.execute(
                select(Match, Opportunity)
                .join(Opportunity, Match.opportunity_id == Opportunity.id)
                .where(Match.watchlist_id == wl.id, Match.status == "new", Opportunity.status == "open")
                .order_by(Opportunity.response_deadline.asc().nullslast())
                .limit(50)
            ).all()
            if not rows:
                continue
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
                        "matched_on": _fmt_matched_on(m.matched_on),
                    }
                )
            total += len(matches)
            groups.append({"watchlist_name": wl.name, "matches": matches})

    return _render(request, "inbox.html", {
        "groups": groups,
        "total_count": total,
        "watchlist_count": len(wls),
        "active_page": "inbox",
    }, user)


def _fmt_matched_on(matched_on: dict | None) -> str:
    if not matched_on:
        return ""
    parts = []
    for k, v in matched_on.items():
        if v:
            parts.append(f"{k}: {v}" if not isinstance(v, list) else f"{k}: {', '.join(str(x) for x in v)}")
    return " · ".join(parts[:3])


def inbox_action(
    request: Request,
    match_id: Annotated[int, Form()],
    action: Annotated[str, Form()],
) -> HTMLResponse:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)

    valid_actions = ("seen", "dismissed", "reviewing", "pursuing")
    if action not in valid_actions:
        return HTMLResponse("", status_code=400)

    with session_scope() as db:
        m = db.get(Match, match_id)
        if m:
            m.status = action
    # HTMX: return empty so the row disappears
    return HTMLResponse("")


# ── Search ────────────────────────────────────────────────────────────────────


def search(
    request: Request,
    q: str | None = None,
    source: str | None = None,
    psc: str | None = None,
    naics: str | None = None,
    status_filter: str = Query("open", alias="status"),
) -> HTMLResponse:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)

    LIMIT = 100
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
        if status_filter != "all":
            stmt = stmt.where(Opportunity.status == status_filter)
        stmt = stmt.order_by(desc(Opportunity.posted_date), desc(Opportunity.id)).limit(LIMIT)
        rows = db.scalars(stmt).all()
        results = []
        for r in rows:
            dl, dlc = deadline_info(r.response_deadline)
            results.append(
                type("R", (), {
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
                })()
            )

    return _render(request, "search.html", {
        "results": results,
        "query": q,
        "source": source,
        "psc": psc,
        "naics": naics,
        "status": status_filter,
        "limit": LIMIT,
        "active_page": "search",
    }, user)


# ── Opportunity detail ────────────────────────────────────────────────────────


def opp_detail(request: Request, opp_id: int) -> HTMLResponse:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)

    with session_scope() as db:
        opp = db.get(Opportunity, opp_id)
        if opp is None:
            return HTMLResponse("Opportunity not found", status_code=404)

        deadline_label, deadline_cls = deadline_info(opp.response_deadline)

        # workspace / pursuit
        pursuit = db.scalar(select(Pursuit).where(Pursuit.opportunity_id == opp_id).order_by(desc(Pursuit.created_at)))
        workspace_id = opp_id if pursuit else None

        # match status
        match = db.scalar(select(Match).where(Match.opportunity_id == opp_id).order_by(desc(Match.created_at)))
        match_status = match.status if match else None

        # ai summary
        ai_summary = db.scalar(
            select(AIAnalysis)
            .where(AIAnalysis.opportunity_id == opp_id, AIAnalysis.analysis_type == "solicitation_summary")
            .order_by(desc(AIAnalysis.created_at))
        )

        # attachments
        attachments = db.scalars(select(StoredFile).where(StoredFile.opportunity_id == opp_id)).all()

        # events
        events = db.scalars(
            select(OpportunityEvent).where(OpportunityEvent.opportunity_id == opp_id)
            .order_by(desc(OpportunityEvent.detected_at)).limit(20)
        ).all()

        # awards
        awards = db.scalars(
            select(Award).where(
                # Match by NSN when the opportunity has one; otherwise match by PSC prefix.
                # Avoid matching all NULL-NSN awards when the opportunity has no NSN.
                Award.nsn == opp.nsn if opp.nsn else Award.psc_code == opp.psc_code
            ).order_by(desc(Award.action_date)).limit(10)
        ).all() if (opp.nsn or opp.psc_code) else []

        # competitors
        competitors = _get_competitors(db, opp)

        # snapshots
        snapshots = db.scalars(
            select(OpportunitySnapshot).where(OpportunitySnapshot.opportunity_id == opp_id)
            .order_by(desc(OpportunitySnapshot.fetched_at)).limit(10)
        ).all()

        # contacts
        contacts = db.scalars(select(Contact).where(Contact.agency_path == opp.agency_path).limit(5)).all() if opp.agency_path else []

        # bid decision
        bid_decision = db.scalar(
            select(BidDecision).where(BidDecision.opportunity_id == opp_id)
            .order_by(desc(BidDecision.created_at))
        )

        external_links = _opportunity_external_links(opp.links)
        source_url = next((url for label, url in external_links if label in {"html_url", "ui"}), None)

        return _render(request, "opp_detail.html", {
            "opp": opp,
            "external_links": external_links,
            "source_url": source_url,
            "deadline_label": deadline_label,
            "deadline_class": deadline_cls,
            "workspace_id": workspace_id,
            "match_status": match_status,
            "ai_summary": ai_summary,
            "attachments": list(attachments),
            "events": list(events),
            "awards": list(awards),
            "competitors": competitors,
            "snapshots": list(snapshots),
            "contacts": list(contacts),
            "bid_decision": bid_decision,
            "active_page": "",
        }, user)


def _get_competitors(db: OrmSession, opp: Opportunity) -> list[Any]:
    """Get competitor info from award history for this opp's NSN/PSC."""
    if not opp.nsn and not opp.psc_code:
        return []
    stmt = (
        select(
            Award.recipient_uei,
            Award.recipient_name,
            func.count(Award.id).label("award_count"),
        )
        .where(
            (Award.nsn == opp.nsn) if opp.nsn else (Award.psc_code == opp.psc_code)
        )
        .group_by(Award.recipient_uei, Award.recipient_name)
        .order_by(desc(func.count(Award.id)))
        .limit(10)
    )
    rows = db.execute(stmt).all()
    return [
        type("C", (), {"recipient_uei": r.recipient_uei, "recipient_name": r.recipient_name, "award_count": r.award_count})()
        for r in rows
    ]


def opp_start_workspace(request: Request, opp_id: int) -> HTMLResponse:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)

    with session_scope() as db:
        opp = db.get(Opportunity, opp_id)
        if opp is None:
            return HTMLResponse("Not found", status_code=404)
        existing = db.scalar(select(Pursuit).where(Pursuit.opportunity_id == opp_id))
        if not existing:
            pursuit = Pursuit(opportunity_id=opp_id, stage="evaluating")
            db.add(pursuit)
    return RedirectResponse(f"/workspace/{opp_id}", status_code=303)


# ── Workspace ─────────────────────────────────────────────────────────────────


def workspace(request: Request, opp_id: int) -> HTMLResponse:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)

    active_tab = request.query_params.get("tab", "overview")
    action_error = request.query_params.get("error")
    action_done = request.query_params.get("done")

    with session_scope() as db:
        opp = db.get(Opportunity, opp_id)
        if opp is None:
            return HTMLResponse("Opportunity not found", status_code=404)

        deadline_label, deadline_cls = deadline_info(opp.response_deadline)
        pursuit = db.scalar(select(Pursuit).where(Pursuit.opportunity_id == opp_id))
        review_session = db.scalar(select(ReviewSession).where(ReviewSession.opportunity_id == opp_id))
        bid_decision = db.scalar(
            select(BidDecision).where(BidDecision.opportunity_id == opp_id).order_by(desc(BidDecision.created_at))
        )
        ai_summary = db.scalar(
            select(AIAnalysis).where(AIAnalysis.opportunity_id == opp_id, AIAnalysis.analysis_type == "solicitation_summary")
            .order_by(desc(AIAnalysis.created_at))
        )

        # build tab-specific context
        tab_ctx: dict[str, Any] = {}

        if active_tab == "requirements" or active_tab == "compliance":
            tab_ctx["requirements"] = db.scalars(
                select(Requirement).where(Requirement.opportunity_id == opp_id)
                .order_by(Requirement.severity.nullslast(), Requirement.id)
                .limit(200)
            ).all()
            tab_ctx["compliance_run"] = db.scalar(
                select(ComplianceRun).where(ComplianceRun.opportunity_id == opp_id)
                .order_by(desc(ComplianceRun.created_at))
            )

        if active_tab == "awards" or active_tab == "market":
            tab_ctx["awards"] = db.scalars(
                select(Award).where(
                    (Award.nsn == opp.nsn) if opp.nsn else (Award.psc_code == opp.psc_code)
                ).order_by(desc(Award.action_date)).limit(50)
            ).all() if (opp.nsn or opp.psc_code) else []

        if active_tab == "market":
            tab_ctx["ai_market"] = db.scalar(
                select(AIAnalysis).where(AIAnalysis.opportunity_id == opp_id, AIAnalysis.analysis_type == "market_analysis")
                .order_by(desc(AIAnalysis.created_at))
            )

        if active_tab == "products":
            from govcon.intelligence.sourcing import supplier_leads
            tab_ctx["supplier_leads"] = supplier_leads(db, opp)
            tab_ctx["ai_sourcing"] = db.scalar(
                select(AIAnalysis).where(AIAnalysis.opportunity_id == opp_id, AIAnalysis.analysis_type == "sourcing_analysis")
                .order_by(desc(AIAnalysis.created_at))
            )

        if active_tab == "pricing":
            from govcon.matching.pricing import recent_award_comps
            tab_ctx["historical_prices"] = recent_award_comps(
                db, nsn=opp.nsn, psc_code=opp.psc_code, limit=20
            )
            tab_ctx["pricing_evidence"] = db.scalar(
                select(AuditEvent).where(
                    AuditEvent.opportunity_id == opp_id,
                    AuditEvent.action_type == "pricing_recorded",
                ).order_by(desc(AuditEvent.created_at), desc(AuditEvent.id))
            )
            tab_ctx["ai_pricing"] = db.scalar(
                select(AIAnalysis).where(AIAnalysis.opportunity_id == opp_id, AIAnalysis.analysis_type == "pricing_analysis")
                .order_by(desc(AIAnalysis.created_at))
            )

        if active_tab == "competitors":
            tab_ctx["competitors"] = _get_competitor_detail(db, opp)

        if active_tab == "ai_decision":
            tab_ctx["decision_package_analysis"] = db.scalar(
                select(AIAnalysis).where(AIAnalysis.opportunity_id == opp_id, AIAnalysis.analysis_type == "decision_package")
                .order_by(desc(AIAnalysis.created_at))
            )

        if active_tab == "review":
            tab_ctx["assignments"] = _get_assignments_with_names(db, opp_id)
            comments_raw = list_comments(db, opportunity_id=opp_id)
            user_map = _user_display_map(db)
            tab_ctx["comments"] = [
                type("C", (), {
                    **{k: getattr(c, k) for k in c.__class__.__table__.columns.keys()},
                    "user_display_name": user_map.get(c.user_id, "?"),
                })()
                for c in comments_raw
            ]
            tab_ctx["can_approve"] = user.role in ("owner", "approver")
            tab_ctx["can_review"] = user.role in ("owner", "approver", "reviewer")
            my_assign = db.scalar(
                select(ReviewAssignment).where(
                    ReviewAssignment.opportunity_id == opp_id,
                    ReviewAssignment.user_id == user.id,
                )
            )
            tab_ctx["my_assignment"] = my_assign

        if active_tab == "proposal":
            proposal = db.scalar(select(Proposal).where(Proposal.opportunity_id == opp_id))
            tab_ctx["proposal"] = proposal
            if proposal and proposal.current_version_id:
                tab_ctx["proposal_version"] = db.get(ProposalVersion, proposal.current_version_id)
            tab_ctx["can_approve"] = user.role in ("owner", "approver")

        if active_tab == "submission":
            proposal = db.scalar(select(Proposal).where(Proposal.opportunity_id == opp_id))
            tab_ctx["proposal"] = proposal
            # Load submission by opportunity_id (Phase 11 generates it that way)
            sub = db.scalar(
                select(Submission).where(Submission.opportunity_id == opp_id)
                .order_by(desc(Submission.created_at))
            )
            # Also check proposal.submission_id as fallback
            if sub is None and proposal and proposal.submission_id:
                sub = db.get(Submission, proposal.submission_id)
            tab_ctx["submission"] = sub
            tab_ctx["submission_authorized"] = db.scalar(
                select(AuditEvent.id).where(
                    AuditEvent.opportunity_id == opp_id,
                    AuditEvent.action_type == "submission_authorized",
                ).order_by(desc(AuditEvent.created_at), desc(AuditEvent.id))
            ) is not None
            tab_ctx["can_approve"] = user.role in ("owner", "approver")

        if active_tab == "activity":
            evts = db.execute(
                select(AuditEvent).where(AuditEvent.opportunity_id == opp_id)
                .order_by(desc(AuditEvent.created_at)).limit(100)
            ).scalars().all()
            user_map = _user_display_map(db)
            tab_ctx["audit_events"] = [
                type("E", (), {
                    **{k: getattr(e, k) for k in e.__class__.__table__.columns.keys()},
                    "user_display_name": user_map.get(e.user_id, "system") if e.user_id else "system",
                })()
                for e in evts
            ]

        ctx: dict[str, Any] = {
            "opp": opp,
            "deadline_label": deadline_label,
            "deadline_class": deadline_cls,
            "pursuit": pursuit,
            "review_session": review_session,
            "bid_decision": bid_decision,
            "ai_summary": ai_summary,
            "active_tab": active_tab,
            "action_error": action_error if action_error in {"missing_inputs", "failed"} else None,
            "action_done": action_done if action_done in {"summary", "compliance", "decision", "proposal", "submission", "pricing", "awards"} else None,
            "active_page": "pipeline",
            **tab_ctx,
        }
        return _render(request, "workspace.html", ctx, user)


_WORKFLOW_TABS = {
    "summary": "overview",
    "compliance": "compliance",
    "decision": "ai_decision",
    "proposal": "proposal",
    "submission": "submission",
}


def workspace_run(
    request: Request, opp_id: int, action: str,
    external_ai_authorized: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    """Run an explicit workflow step from the workspace with its existing gates."""
    import logging
    from govcon.audit import record_audit
    from govcon.config import get_settings

    if action not in _WORKFLOW_TABS:
        return HTMLResponse("Unknown workflow action", status_code=404)
    tab = _WORKFLOW_TABS[action]
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    try:
        require_permission(user, "approve" if action in {"proposal", "submission"} else "review")
    except PermissionDenied:
        return HTMLResponse("You do not have permission for this action", status_code=403)
    if action in {"summary", "compliance", "decision", "proposal"} and external_ai_authorized != "yes":
        return HTMLResponse("Confirm external AI processing before running this step", status_code=400)

    settings = get_settings()
    try:
        with session_scope() as db:
            opp = db.get(Opportunity, opp_id)
            if opp is None:
                return HTMLResponse("Opportunity not found", status_code=404)
            if action == "summary":
                from govcon.enrich.summarize import run_solicitation_analysis
                if run_solicitation_analysis(db, opp, settings=settings) is None:
                    raise ValueError("missing_inputs")
            elif action == "compliance":
                from govcon.compliance.pipeline import run_compliance_pipeline
                run_compliance_pipeline(db, opp_id, settings=settings)
            elif action == "decision":
                from govcon.decision.engine import run_preliminary_decision_package
                run_preliminary_decision_package(db, opportunity_id=opp_id, settings=settings)
            elif action == "proposal":
                from govcon.proposals.service import generate_proposal
                generate_proposal(db, opportunity_id=opp_id, actor=user, settings=settings)
            elif action == "submission":
                from govcon.submissions.service import generate_submission_package
                generate_submission_package(db, opportunity_id=opp_id, actor=user, settings=settings)
            record_audit(db, user_id=user.id, opportunity_id=opp_id,
                         action_type=f"workspace_{action}_run", entity_type="opportunity", entity_id=opp_id)
    except ValueError as exc:
        logging.getLogger("govcon.web.routes").warning("workflow %s for %s: %s", action, opp_id, exc)
        return RedirectResponse(f"/workspace/{opp_id}?tab={tab}&error=missing_inputs", status_code=303)
    except Exception:
        logging.getLogger("govcon.web.routes").exception("workflow %s failed for %s", action, opp_id)
        return RedirectResponse(f"/workspace/{opp_id}?tab={tab}&error=failed", status_code=303)
    return RedirectResponse(f"/workspace/{opp_id}?tab={tab}&done={action}", status_code=303)


def workspace_pricing_save(
    request: Request, opp_id: int,
    supplier: Annotated[str, Form()],
    sourcing_cost: Annotated[str, Form()],
    quote_price: Annotated[str, Form()],
    evidence_reference: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Record human-verified supplier cost and proposed bid price with provenance."""
    from govcon.audit import record_audit

    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    try:
        require_permission(user, "review")
    except PermissionDenied:
        return HTMLResponse("You do not have permission for this action", status_code=403)
    supplier = supplier.strip()
    evidence_reference = evidence_reference.strip()
    try:
        cost, price = Decimal(sourcing_cost), Decimal(quote_price)
    except InvalidOperation:
        return HTMLResponse("Enter valid amounts", status_code=400)
    if not supplier or not evidence_reference or len(evidence_reference) > 1000 or not cost.is_finite() or not price.is_finite() or cost <= 0 or price < 0:
        return HTMLResponse("Supplier, evidence reference, and valid amounts are required", status_code=400)
    with session_scope() as db:
        pursuit = db.scalar(select(Pursuit).where(Pursuit.opportunity_id == opp_id))
        if pursuit is None:
            return HTMLResponse("Start a workspace before recording pricing", status_code=400)
        old = {"supplier": pursuit.supplier, "sourcing_cost": str(pursuit.sourcing_cost) if pursuit.sourcing_cost is not None else None,
               "quote_price": str(pursuit.quote_price) if pursuit.quote_price is not None else None}
        pursuit.supplier = supplier
        pursuit.sourcing_cost = cost
        pursuit.quote_price = price
        record_audit(db, action_type="pricing_recorded", user_id=user.id,
                     opportunity_id=opp_id, entity_type="pursuit", entity_id=pursuit.id,
                     old_value=old, new_value={"supplier": supplier, "sourcing_cost": str(cost),
                                               "quote_price": str(price), "evidence_reference": evidence_reference})
    return RedirectResponse(f"/workspace/{opp_id}?tab=pricing&done=pricing", status_code=303)


def workspace_award_sample(request: Request, opp_id: int) -> HTMLResponse:
    """Add public award history for a selected opportunity without a watchlist."""
    import logging
    from govcon.audit import record_audit
    from govcon.ingest.usaspending import ingest_award_sample_for_opportunity

    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    try:
        require_permission(user, "review")
    except PermissionDenied:
        return HTMLResponse("You do not have permission for this action", status_code=403)
    try:
        with session_scope() as db:
            opp = db.get(Opportunity, opp_id)
            if opp is None:
                return HTMLResponse("Opportunity not found", status_code=404)
            stats, more = ingest_award_sample_for_opportunity(db, opp)
            record_audit(db, action_type="award_sample_ingested", user_id=user.id,
                         opportunity_id=opp_id, entity_type="opportunity", entity_id=opp_id,
                         new_value={"fetched": stats.fetched, "more_pages_available": more})
    except ValueError:
        return RedirectResponse(f"/workspace/{opp_id}?tab=awards&error=missing_inputs", status_code=303)
    except Exception:
        logging.getLogger("govcon.web.routes").exception("award sample failed for %s", opp_id)
        return RedirectResponse(f"/workspace/{opp_id}?tab=awards&error=failed", status_code=303)
    return RedirectResponse(f"/workspace/{opp_id}?tab=awards&done=awards", status_code=303)


def _get_competitor_detail(db: OrmSession, opp: Opportunity) -> list[Any]:
    if not opp.nsn and not opp.psc_code:
        return []
    stmt = (
        select(
            Award.recipient_uei,
            Award.recipient_name,
            func.count(Award.id).label("award_count"),
            func.sum(Award.total_obligation).label("total_value"),
        )
        .where((Award.nsn == opp.nsn) if opp.nsn else (Award.psc_code == opp.psc_code))
        .group_by(Award.recipient_uei, Award.recipient_name)
        .order_by(desc(func.count(Award.id)))
        .limit(20)
    )
    rows = db.execute(stmt).all()
    return [
        type("C", (), {
            "recipient_uei": r.recipient_uei,
            "recipient_name": r.recipient_name,
            "award_count": r.award_count,
            "total_value": r.total_value,
        })()
        for r in rows
    ]


def _get_assignments_with_names(db: OrmSession, opp_id: int) -> list[Any]:
    rows = db.execute(
        select(ReviewAssignment, User)
        .join(User, ReviewAssignment.user_id == User.id)
        .where(ReviewAssignment.opportunity_id == opp_id)
    ).all()
    return [
        type("A", (), {
            **{k: getattr(a, k) for k in a.__class__.__table__.columns.keys()},
            "user_display_name": u.display_name,
        })()
        for a, u in rows
    ]


def _user_display_map(db: OrmSession) -> dict[int, str]:
    rows = db.execute(select(User.id, User.display_name)).all()
    return {r.id: r.display_name for r in rows}


def workspace_comment(
    request: Request,
    opp_id: int,
    body: Annotated[str, Form()],
    user_recommendation: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    try:
        require_permission(user, "review")
    except PermissionDenied:
        return RedirectResponse(f"/workspace/{opp_id}?tab=review", status_code=303)

    with session_scope() as db:
        comment = ReviewComment(
            opportunity_id=opp_id,
            user_id=user.id,
            body=body,
            user_recommendation=user_recommendation or None,
        )
        db.add(comment)
    return RedirectResponse(f"/workspace/{opp_id}?tab=review", status_code=303)


def workspace_complete_review(
    request: Request,
    opp_id: int,
    recommendation: Annotated[str, Form()],
) -> HTMLResponse:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)

    with session_scope() as db:
        assignment = db.scalar(
            select(ReviewAssignment).where(
                ReviewAssignment.opportunity_id == opp_id,
                ReviewAssignment.user_id == user.id,
            )
        )
        if assignment:
            assignment.status = "complete"
            assignment.recommendation = recommendation
            assignment.completed_at = datetime.now(UTC)
        # Update review session count
        rs = db.scalar(select(ReviewSession).where(ReviewSession.opportunity_id == opp_id))
        if rs:
            rs.completed_review_count = (rs.completed_review_count or 0) + 1
            if rs.completed_review_count >= rs.required_review_count:
                rs.status = "approval_pending"
    return RedirectResponse(f"/workspace/{opp_id}?tab=review", status_code=303)


def workspace_approve(
    request: Request,
    opp_id: int,
    decision: Annotated[str, Form()],
) -> HTMLResponse:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    try:
        require_permission(user, "approve")
    except PermissionDenied:
        return RedirectResponse(f"/workspace/{opp_id}?tab=review", status_code=303)

    with session_scope() as db:
        rs = db.scalar(select(ReviewSession).where(ReviewSession.opportunity_id == opp_id))
        if rs:
            if decision == "approve_to_bid":
                rs.status = "approved_to_bid"
                rs.final_approval_status = "approved_to_bid"
                rs.approved_by_user_id = user.id
                rs.approved_at = datetime.now(UTC)
                # create/update pursuit
                pursuit = db.scalar(select(Pursuit).where(Pursuit.opportunity_id == opp_id))
                if not pursuit:
                    pursuit = Pursuit(opportunity_id=opp_id, stage="bid_approved")
                    db.add(pursuit)
                    db.flush()
                else:
                    pursuit.stage = "bid_approved"
                    pursuit.approved_to_bid_at = datetime.now(UTC)
                    db.flush()

                # ── Phase 11 auto-generation ──────────────────────────────────
                # Generate proposal draft immediately; skip_ai=True when no key
                # is configured so the UI always shows real draft state, not
                # "forever generating…".
                _trigger_proposal_generation(db, opp_id=opp_id, actor=user)

            elif decision == "return_for_review":
                rs.status = "returned_for_review"
                rs.final_approval_status = "returned_for_review"
            elif decision == "no_bid":
                rs.status = "no_bid"
                rs.final_approval_status = "no_bid"
    return RedirectResponse(f"/workspace/{opp_id}?tab=review", status_code=303)


def _trigger_proposal_generation(db: Any, *, opp_id: int, actor: Any) -> None:
    """Call Phase 11 generate_proposal + generate_submission_package.

    Generates a placeholder draft with visible blockers. AI drafting requires
    a separate explicit workspace action and external processing confirmation.
    Falls back gracefully if the services raise; errors are logged, not raised.
    """
    import logging
    _log = logging.getLogger("govcon.web.routes")
    try:
        from govcon.proposals.service import generate_proposal
        from govcon.submissions.service import generate_submission_package
        generate_proposal(db, opportunity_id=opp_id, actor=actor, skip_ai=True)
        generate_submission_package(db, opportunity_id=opp_id, actor=actor)
    except Exception as exc:
        _log.warning("auto-generate on approve_to_bid failed for opp %s: %s", opp_id, exc)


def workspace_proposal_approve(
    request: Request,
    opp_id: int,
    decision: Annotated[str, Form()],
    override_reason: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    try:
        require_permission(user, "approve")
    except PermissionDenied:
        return RedirectResponse(f"/workspace/{opp_id}?tab=proposal", status_code=303)

    from govcon.proposals.service import finalize_proposal
    from govcon.compliance.submission_preflight import ReadinessBlocked
    try:
        with session_scope() as db:
            finalize_proposal(db, opportunity_id=opp_id, action=decision,
                              actor=user, override_reason=(override_reason or None))
    except (ReadinessBlocked, ValueError):
        return RedirectResponse(f"/workspace/{opp_id}?tab=proposal&error=missing_inputs", status_code=303)
    return RedirectResponse(f"/workspace/{opp_id}?tab=proposal", status_code=303)


def workspace_submission_approve(
    request: Request,
    opp_id: int,
    decision: Annotated[str, Form()],
    confirmation_number: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    try:
        require_permission(user, "approve")
    except PermissionDenied:
        return RedirectResponse(f"/workspace/{opp_id}?tab=submission", status_code=303)

    with session_scope() as db:
        # Find submission by opportunity_id (preferred) or proposal.submission_id
        sub = db.scalar(
            select(Submission).where(Submission.opportunity_id == opp_id)
            .order_by(desc(Submission.created_at))
        )
        if sub is None:
            proposal = db.scalar(select(Proposal).where(Proposal.opportunity_id == opp_id))
            if proposal and proposal.submission_id:
                sub = db.get(Submission, proposal.submission_id)
        proposal = db.scalar(select(Proposal).where(Proposal.opportunity_id == opp_id))
        if sub is None or proposal is None or proposal.status != "final_approved" or sub.status != "ready":
            return HTMLResponse("A ready package and final approved proposal are required", status_code=400)
        if decision == "approve":
            from govcon.audit import record_audit
            record_audit(db, action_type="submission_authorized", user_id=user.id,
                         opportunity_id=opp_id, entity_type="submission", entity_id=sub.id)
        elif decision == "record_submitted":
            authorized = db.scalar(select(AuditEvent.id).where(
                AuditEvent.opportunity_id == opp_id,
                AuditEvent.action_type == "submission_authorized",
            ))
            reference = (confirmation_number or "").strip()
            if authorized is None or not reference:
                return HTMLResponse("Authorization and a real confirmation reference are required", status_code=400)
            from govcon.proposals.service import record_submission_confirmation
            record_submission_confirmation(db, opportunity_id=opp_id,
                                           confirmation_number=reference, actor=user)
        else:
            return HTMLResponse("Unknown submission action", status_code=400)
    return RedirectResponse(f"/workspace/{opp_id}?tab=submission", status_code=303)


def workspace_record_outcome(
    request: Request,
    opp_id: int,
    outcome: Annotated[str, Form()],
    # No-bid
    no_bid_reason: Annotated[str | None, Form()] = None,
    no_bid_category: Annotated[str | None, Form()] = None,
    # Loss
    loss_reason: Annotated[str | None, Form()] = None,
    known_winning_price: Annotated[str | None, Form()] = None,
    # Win
    win_reason: Annotated[str | None, Form()] = None,
    win_margin_pct: Annotated[str | None, Form()] = None,
    win_supplier: Annotated[str | None, Form()] = None,
    win_delivery_terms: Annotated[str | None, Form()] = None,
    win_proposal_version: Annotated[str | None, Form()] = None,
    # Common
    awarded_vendor_name: Annotated[str | None, Form()] = None,
    awarded_vendor_uei: Annotated[str | None, Form()] = None,
    award_amount: Annotated[str | None, Form()] = None,
    government_feedback: Annotated[str | None, Form()] = None,
    debrief_notes: Annotated[str | None, Form()] = None,
    lessons_learned: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    try:
        _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)

    valid_outcomes = ("won", "lost", "no_bid", "cancelled")
    if outcome not in valid_outcomes:
        return RedirectResponse(f"/workspace/{opp_id}?tab=submission", status_code=303)

    def _float(val: str | None) -> float | None:
        try:
            return float(val) if val and val.strip() else None
        except ValueError:
            return None

    with session_scope() as db:
        try:
            record_outcome(
                db,
                opportunity_id=opp_id,
                outcome=outcome,
                no_bid_reason=no_bid_reason or None,
                no_bid_category=no_bid_category or None,
                loss_reason=loss_reason or None,
                known_winning_price=_float(known_winning_price),
                win_reason=win_reason or None,
                win_margin_pct=_float(win_margin_pct),
                win_supplier=win_supplier or None,
                win_delivery_terms=win_delivery_terms or None,
                win_proposal_version=win_proposal_version or None,
                awarded_vendor_name=awarded_vendor_name or None,
                awarded_vendor_uei=awarded_vendor_uei or None,
                award_amount=_float(award_amount),
                government_feedback=government_feedback or None,
                debrief_notes=debrief_notes or None,
                lessons_learned=lessons_learned or None,
            )
        except ValueError:
            pass
    return RedirectResponse(f"/workspace/{opp_id}?tab=submission", status_code=303)


# ── Pipeline ──────────────────────────────────────────────────────────────────

_PIPELINE_COLUMNS = [
    ("ingested",            "Ingested"),
    ("evaluating",          "AI Analyzing"),
    ("sourcing",            "Sourcing / Pricing"),
    ("compliance",          "Compliance"),
    ("ready_for_review",    "Ready for Review"),
    ("under_review",        "Collaborative Review"),
    ("approval_pending",    "Approval Pending"),
    ("bid_approved",        "Approved to Bid"),
    ("drafting",            "Generating Proposal"),
    ("review",              "Submission Validation"),
    ("ready_to_submit",     "Ready to Submit"),
    ("submitted",           "Submitted"),
    ("won",                 "Won"),
    ("lost",                "Lost"),
    ("no_bid",              "No Bid"),
]


def pipeline(request: Request) -> HTMLResponse:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)

    with session_scope() as db:
        # Build a map: opp_id -> pursuit stage (or "ingested" for no pursuit)
        pursuits = db.execute(select(Pursuit, Opportunity).join(Opportunity, Pursuit.opportunity_id == Opportunity.id)).all()
        # Also include recently-matched opps with no pursuit
        matched_opp_ids = set(p.opportunity_id for p, _ in pursuits)
        new_matches = db.execute(
            select(Match, Opportunity)
            .join(Opportunity, Match.opportunity_id == Opportunity.id)
            .where(Match.status.in_(["new", "seen", "reviewing"]))
            .where(Opportunity.status == "open")
            .where(~Match.opportunity_id.in_(list(matched_opp_ids) or [-1]))
            .limit(50)
        ).all()

        stage_cards: dict[str, list[dict]] = {k: [] for k, _ in _PIPELINE_COLUMNS}

        for p, opp in pursuits:
            stage = p.stage or "evaluating"
            # Map review session statuses to pipeline stage
            rs = db.scalar(select(ReviewSession).where(ReviewSession.opportunity_id == opp.id))
            if rs:
                if rs.status == "under_review":
                    stage = "under_review"
                elif rs.status == "approval_pending":
                    stage = "approval_pending"
                elif rs.status == "approved_to_bid":
                    stage = "bid_approved"
                elif rs.status == "no_bid":
                    stage = "no_bid"
            dl, dlc = deadline_info(opp.response_deadline)
            card = {
                "opp_id": opp.id,
                "title": opp.title,
                "source_id": opp.source_id,
                "deadline_label": dl,
                "deadline_class": dlc,
                "value": format_value(opp.estimated_value_min, opp.estimated_value_max),
                "margin": p.margin_pct,
            }
            if stage in stage_cards:
                stage_cards[stage].append(card)
            else:
                stage_cards["evaluating"].append(card)

        for m, opp in new_matches:
            dl, dlc = deadline_info(opp.response_deadline)
            stage_cards["ingested"].append({
                "opp_id": opp.id,
                "title": opp.title,
                "source_id": opp.source_id,
                "deadline_label": dl,
                "deadline_class": dlc,
                "value": format_value(opp.estimated_value_min, opp.estimated_value_max),
                "margin": None,
            })

        columns = [
            {"key": k, "label": label, "cards": stage_cards.get(k, [])}
            for k, label in _PIPELINE_COLUMNS
        ]
        total = sum(len(c["cards"]) for c in columns)

    return _render(request, "pipeline.html", {"columns": columns, "total_count": total, "active_page": "pipeline"}, user)


# ── Watchlists ────────────────────────────────────────────────────────────────


def watchlists(request: Request) -> HTMLResponse:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)

    with session_scope() as db:
        wls = db.scalars(select(Watchlist).order_by(Watchlist.name)).all()
        return _render(request, "watchlists.html", {"watchlists": list(wls), "active_page": "watchlists"}, user)


def watchlist_new_get(request: Request) -> HTMLResponse:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    return _render(request, "watchlist_edit.html", {"editing": False, "wl": None, "active_page": "watchlists"}, user)


async def watchlist_new_post(request: Request) -> HTMLResponse:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)

    return await _watchlist_save(request, user, wl_id=None)


def watchlist_edit_get(request: Request, wl_id: int) -> HTMLResponse:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)

    with session_scope() as db:
        wl = db.get(Watchlist, wl_id)
        if wl is None:
            return HTMLResponse("Not found", status_code=404)
        return _render(request, "watchlist_edit.html", {"editing": True, "wl": wl, "active_page": "watchlists"}, user)


async def watchlist_edit_post(request: Request, wl_id: int) -> HTMLResponse:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    return await _watchlist_save(request, user, wl_id=wl_id)


async def _watchlist_save(request: Request, user: User, wl_id: int | None) -> HTMLResponse:
    form = await request.form()
    name = (form.get("name") or "").strip()

    def invalid_form(message: str) -> HTMLResponse:
        with session_scope() as db:
            current = db.get(Watchlist, wl_id) if wl_id is not None else None
        ctx = {
            "editing": wl_id is not None,
            "wl": current,
            "error": message,
            "active_page": "watchlists",
        }
        return _render(request, "watchlist_edit.html", ctx, user)

    if not name:
        return invalid_form("Name is required.")

    def parse_list(val: str | None) -> list[str] | None:
        if not val or not val.strip():
            return None
        return [x.strip() for x in val.split(",") if x.strip()]

    def parse_num(val: str | None):
        try:
            return float(val) if val and val.strip() else None
        except ValueError:
            return None

    criteria = (
        "psc_codes", "naics_codes", "keywords", "nsn_list", "set_asides", "sources",
    )
    if not any(parse_list(form.get(key)) for key in criteria) and not any(
        form.get(key) and form.get(key).strip()
        for key in ("min_value", "max_value", "min_deadline_days")
    ):
        return invalid_form("Add at least one code, keyword, source, value, or deadline filter.")

    with session_scope() as db:
        if wl_id:
            wl = db.get(Watchlist, wl_id)
            if wl is None:
                return HTMLResponse("Not found", status_code=404)
        else:
            wl = Watchlist()
            db.add(wl)

        wl.name = name
        wl.psc_codes = parse_list(form.get("psc_codes"))
        wl.naics_codes = parse_list(form.get("naics_codes"))
        wl.keywords = parse_list(form.get("keywords"))
        wl.exclude_keywords = parse_list(form.get("exclude_keywords"))
        wl.nsn_list = parse_list(form.get("nsn_list"))
        wl.set_asides = parse_list(form.get("set_asides"))
        wl.min_value = parse_num(form.get("min_value"))
        wl.max_value = parse_num(form.get("max_value"))
        wl.min_deadline_days = int(form.get("min_deadline_days")) if form.get("min_deadline_days") and form.get("min_deadline_days").strip() else None
        wl.sources = parse_list(form.get("sources"))
        wl.notes = (form.get("notes") or "").strip() or None

        db.flush()
        from govcon.matching.engine import run_matching
        run_matching(db, watchlist_id=wl.id, rebuild=True)

    return RedirectResponse("/watchlists", status_code=303)


def watchlist_toggle(
    request: Request,
    wl_id: int,
    enabled: Annotated[str, Form()],
) -> HTMLResponse:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)

    with session_scope() as db:
        wl = db.get(Watchlist, wl_id)
        if wl:
            wl.enabled = enabled.lower() == "true"
    return RedirectResponse("/watchlists", status_code=303)


def watchlist_rebuild(request: Request, wl_id: int) -> HTMLResponse:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)

    try:
        from govcon.matching.engine import run_matching
        with session_scope() as db:
            run_matching(db, watchlist_id=wl_id)
    except Exception:
        pass

    return RedirectResponse("/watchlists", status_code=303)


# ── Vendors ───────────────────────────────────────────────────────────────────


def vendors(request: Request) -> HTMLResponse:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)

    q = request.query_params.get("q", "").strip()
    vendor = None
    vendor_awards = None
    results = None
    error = None

    if q:
        with session_scope() as db:
            # Try exact UEI match first
            vendor = db.scalar(select(Vendor).where(Vendor.uei == q.upper()))
            if vendor is None:
                # Try CAGE
                vendor = db.scalar(select(Vendor).where(Vendor.cage_code == q.upper()))
            if vendor is None and len(q) == 12 and q.isalnum():
                from govcon.ingest.sam_entities import SamEntityError, ensure_vendor
                try:
                    vendor, _ = ensure_vendor(db, q)
                except (SamEntityError, ValueError):
                    error = "SAM.gov entity lookup is unavailable or the UEI was not found. Please try again later."
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

    awardee_name = Opportunity.raw["award"]["awardee"]["name"].astext
    with session_scope() as db:
        awardee_query = (
            select(
                awardee_name.label("name"),
                func.count(Opportunity.id).label("notice_count"),
                func.max(Opportunity.id).label("opp_id"),
            )
            .where(Opportunity.source == "sam", awardee_name.is_not(None), awardee_name != "")
            .group_by(awardee_name)
            .order_by(desc(func.count(Opportunity.id)))
            .limit(30)
        )
        if q:
            awardee_query = awardee_query.where(awardee_name.ilike(f"%{q}%"))
        awardees = db.execute(awardee_query).all()

    return _render(request, "vendors.html", {
        "query": q or None,
        "vendor": vendor,
        "vendor_awards": vendor_awards,
        "results": results,
        "awardees": awardees,
        "error": error,
        "active_page": "vendors",
    }, user)


# ── Operations ────────────────────────────────────────────────────────────────


def ops(request: Request) -> HTMLResponse:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)

    from govcon.scheduler.chains import CHAIN_DEFINITIONS

    with session_scope() as db:
        opp_count = db.scalar(select(func.count()).select_from(Opportunity)) or 0
        opp_open = db.scalar(select(func.count()).select_from(Opportunity).where(Opportunity.status == "open")) or 0
        match_count = db.scalar(select(func.count()).select_from(Match)) or 0
        match_new = db.scalar(select(func.count()).select_from(Match).where(Match.status == "new")) or 0
        match_pursuing = db.scalar(select(func.count()).select_from(Match).where(Match.status == "pursuing")) or 0
        user_count = db.scalar(select(func.count()).select_from(User).where(User.is_active == True)) or 0

        runs = db.scalars(
            select(IngestionRun).order_by(desc(IngestionRun.started_at)).limit(30)
        ).all()

        # Scheduler job runs — latest per chain + last 20 overall
        job_runs = db.scalars(
            select(SchedulerJobRun).order_by(desc(SchedulerJobRun.started_at)).limit(50)
        ).all()

        # Latest run per chain for the summary panel
        chain_summary = []
        for chain_name, chain_def in CHAIN_DEFINITIONS.items():
            last = db.scalars(
                select(SchedulerJobRun)
                .where(SchedulerJobRun.chain_name == chain_name)
                .order_by(desc(SchedulerJobRun.started_at))
                .limit(1)
            ).first()
            chain_summary.append({
                "name": chain_name,
                "description": chain_def.description,
                "cron": chain_def.cron,
                "steps": chain_def.steps,
                "last_run": last,
            })

        users = db.scalars(select(User).order_by(User.email)).all() if user.role == "owner" else []

    stats = type("S", (), {
        "opp_count": opp_count,
        "opp_open": opp_open,
        "match_count": match_count,
        "match_new": match_new,
        "match_pursuing": match_pursuing,
        "user_count": user_count,
    })()

    return _render(request, "ops.html", {
        "stats": stats,
        "runs": list(runs),
        "job_runs": list(job_runs),
        "chain_summary": chain_summary,
        "users": list(users),
        "active_page": "ops",
    }, user)


# ── Learning ──────────────────────────────────────────────────────────────────


def learning(request: Request) -> HTMLResponse:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)

    with session_scope() as db:
        analytics = outcome_analytics(db)

    stats = type("S", (), {
        "submitted": analytics.total_submitted,
        "won": analytics.total_won,
        "lost": analytics.total_lost,
        "no_bid": analytics.total_no_bid,
        "win_rate": analytics.overall_win_rate_pct or 0,
        "avg_margin": analytics.avg_margin_pct_on_wins,
        "avg_days_cycle": analytics.avg_days_discovery_to_submission,
        "win_profile_available": analytics.win_profile_available,
        "win_profile_note": analytics.win_profile_note,
        "win_profile_minimum": WIN_PROFILE_MINIMUM,
    })()

    return _render(request, "learning.html", {
        "stats": stats,
        "by_psc": analytics.by_psc,
        "by_agency": analytics.by_agency,
        "by_size": analytics.by_size_bucket,
        "no_bid_reasons": analytics.no_bid_reasons,
        "loss_reasons": analytics.loss_reasons,
        "common_competitors": analytics.common_competitors,
        "reliable_suppliers": analytics.reliable_suppliers,
        "recent_outcomes": analytics.recent_outcomes,
        "no_bid_categories": sorted(NO_BID_CATEGORIES),
        "common_compliance_issues": [],
        "active_page": "learning",
    }, user)


# ── Admin / users ─────────────────────────────────────────────────────────────


def admin_invite_get(request: Request) -> HTMLResponse:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    try:
        require_permission(user, "manage_users")
    except PermissionDenied:
        return RedirectResponse("/ops", status_code=303)
    return _render(request, "invite_user.html", {"active_page": "ops"}, user)


async def admin_invite_post(request: Request) -> HTMLResponse:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    try:
        require_permission(user, "manage_users")
    except PermissionDenied:
        return RedirectResponse("/ops", status_code=303)

    form = await request.form()
    email = (form.get("email") or "").strip()
    display_name = (form.get("display_name") or "").strip()
    password = (form.get("password") or "")
    role = (form.get("role") or "reviewer").strip()

    try:
        with session_scope() as db:
            invite_user(db, email=email, display_name=display_name, password=password, role=role, actor_user_id=user.id)
        return _render(request, "invite_user.html", {"success": f"User {email} invited.", "active_page": "ops"}, user)
    except (ValueError, AuthError) as exc:
        return _render(request, "invite_user.html", {"error": str(exc), "active_page": "ops"}, user)
