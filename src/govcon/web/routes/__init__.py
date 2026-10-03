"""Phase 14 FastAPI routes — all product pages."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import Cookie, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select, text, or_
from sqlalchemy.orm import Session as OrmSession

from govcon.ai.analysis_types import AnalysisType
from govcon.audit import record_audit
from govcon.collaboration.assignments import assign_reviewer
from govcon.collaboration.comments import add_comment, list_comments
from govcon.collaboration.review_sessions import (
    ReviewWorkflowError,
    approval_context,
    complete_assignment,
    ensure_review_session,
    finalize_approval,
    recalculate_quorum,
)
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
from govcon.compliance.submission_preflight import ReadinessBlocked
from govcon.concurrency import StaleRecordError
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
from govcon.proposals.service import (
    finalize_proposal,
    get_proposal_workspace,
    record_submission_confirmation,
)
from govcon.web.helpers import deadline_info, format_value, primary_source_url, source_links
from govcon.web.security import secure_cookies
from govcon.workflow.transitions import APPROVABLE_PROPOSAL_STATUSES

# Errors a workflow service raises to refuse an action. They become a message
# for the user; the request's transaction is rolled back.
_WORKFLOW_ERRORS = (ValueError, RuntimeError, AuthError, StaleRecordError)

import pathlib

_TEMPLATE_DIR = pathlib.Path(__file__).parent.parent / "templates"
_templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

# ── Deadline helper exposed to templates ──────────────────────────────────────


def _money(value: Any, places: int = 2) -> str:
    """``$1,234.50`` — Python ``%`` formatting has no thousands separator."""
    try:
        return f"${float(value):,.{int(places)}f}"
    except (TypeError, ValueError):
        return "—"


def _add_template_globals(templates: Jinja2Templates) -> None:
    from govcon.web.security import csrf_token
    templates.env.globals["csrf_token"] = csrf_token
    templates.env.globals["now"] = lambda: datetime.now(UTC)
    templates.env.filters["money"] = _money


_add_template_globals(_templates)


# ── Cookie / auth helpers ──────────────────────────────────────────────────────

_COOKIE_NAME = "govcon_session"


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


def _redirect(url: str, *, error: str | None = None, notice: str | None = None) -> RedirectResponse:
    """303 redirect carrying an optional message for the next page."""
    params = []
    if error:
        params.append("error=" + quote(error[:600]))
    if notice:
        params.append("notice=" + quote(notice[:300]))
    if params:
        url = url + ("&" if "?" in url else "?") + "&".join(params)
    return RedirectResponse(url, status_code=303)


def _error_text(exc: Exception) -> str:
    if isinstance(exc, ReadinessBlocked):
        lines = [b.get("description", "") for b in exc.blockers]
        return "Submission readiness is blocked: " + " | ".join(lines)
    if isinstance(exc, PermissionDenied):
        return f"Not permitted: {exc}"
    return str(exc) or exc.__class__.__name__


def _actor(db: OrmSession, user: User, permission: str) -> User:
    """Reload the logged-in user in this transaction and check the permission."""
    actor = db.get(User, user.id)
    if actor is None or not actor.is_active:
        raise PermissionDenied("account is inactive")
    require_permission(actor, permission)
    return actor


def _form_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None and str(value).strip() else None
    except ValueError:
        return None


def _render(request: Request, template: str, ctx: dict[str, Any], user: User) -> HTMLResponse:
    ctx["current_user"] = user
    ctx["unread_notification_count"] = _unread_count(user)
    return _templates.TemplateResponse(request, template, ctx)


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
        raw_token = create_session(db, user, settings=request.app.state.settings)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        _COOKIE_NAME,
        raw_token,
        max_age=request.app.state.settings.session_ttl_hours * 3600,
        httponly=True,
        secure=secure_cookies(request),
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


def notifications(request: Request, before: Annotated[int | None, Query(ge=1)] = None) -> HTMLResponse:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    with session_scope() as db:
        query = select(Notification).where(Notification.user_id == user.id)
        if before is not None:
            query = query.where(Notification.id < before)
        rows = db.scalars(query.order_by(Notification.id.desc()).limit(51)).all()
        next_before = rows[49].id if len(rows) > 50 else None
        items = [{"id": row.id, "title": row.notification_type.replace("_", " ").capitalize(),
                  "opportunity_id": row.opportunity_id, "created_at": row.created_at,
                  "payload": json.dumps(row.payload or {}, ensure_ascii=False, default=str),
                  "unread": row.read_at is None} for row in rows[:50]]
    return _render(request, "notifications.html", {"notifications": items, "next_before": next_before, "active_page": "notifications"}, user)


def notification_read(request: Request, notification_id: int) -> RedirectResponse:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    with session_scope() as db:
        row = db.scalar(select(Notification).where(
            Notification.id == notification_id, Notification.user_id == user.id).with_for_update())
        if row is None:
            raise HTTPException(status_code=404, detail="Notification not found")
        if row.read_at is None:
            row.read_at = datetime.now(UTC)
    return _redirect("/notifications")


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
                .where(Match.watchlist_id == wl.id, Match.status == "new", Match.active.is_(True))
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
                        "alerted": m.alerted_at is not None,
                    }
                )
            total += len(matches)
            groups.append({"watchlist_name": wl.name, "matches": matches})

    return _render(request, "inbox.html", {"groups": groups, "total_count": total, "active_page": "inbox"}, user)


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

    try:
        with session_scope() as db:
            actor = _actor(db, user, "review")
            m = db.get(Match, match_id)
            if m is None:
                return HTMLResponse("", status_code=404)
            if action == "pursuing":
                from govcon.workflow.invalidation import lock_opportunity

                lock_opportunity(db, m.opportunity_id)
                pursuit = db.scalar(select(Pursuit).where(Pursuit.opportunity_id == m.opportunity_id))
                if pursuit is None:
                    pursuit = Pursuit(opportunity_id=m.opportunity_id, stage="evaluating")
                    db.add(pursuit)
                    db.flush()
                    record_audit(
                        db,
                        action_type="pursuit_created",
                        user_id=actor.id,
                        opportunity_id=m.opportunity_id,
                        entity_type="pursuits",
                        entity_id=pursuit.id,
                        new_value={"stage": "evaluating"},
                    )
            old = m.status
            m.status = action
            record_audit(
                db,
                action_type="match_status_changed",
                user_id=actor.id,
                opportunity_id=m.opportunity_id,
                entity_type="matches",
                entity_id=m.id,
                old_value={"status": old},
                new_value={"status": action},
            )
    except PermissionDenied:
        return HTMLResponse("Not permitted", status_code=403)
    # HTMX: return empty so the row disappears
    return HTMLResponse("")


# ── Search ────────────────────────────────────────────────────────────────────


def search(
    request: Request,
    q: str | None = None,
    source: str | None = None,
    psc: str | None = None,
    naics: str | None = None,
    status_filter: str | None = Query(None, alias="status"),
) -> HTMLResponse:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)

    LIMIT = 100
    results = None

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
            stmt = stmt.order_by(desc(Opportunity.response_deadline)).limit(LIMIT)
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
            .where(AIAnalysis.opportunity_id == opp_id, AIAnalysis.analysis_type == AnalysisType.SOLICITATION_SUMMARY)
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

        return _render(request, "opp_detail.html", {
            "opp": opp,
            "source_links": source_links(opp.links),
            "primary_source_url": primary_source_url(opp.links),
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

    try:
        with session_scope() as db:
            actor = _actor(db, user, "review")
            opp = db.get(Opportunity, opp_id)
            if opp is None:
                return HTMLResponse("Not found", status_code=404)
            existing = db.scalar(select(Pursuit).where(Pursuit.opportunity_id == opp_id))
            if not existing:
                pursuit = Pursuit(opportunity_id=opp_id, stage="evaluating")
                db.add(pursuit)
                db.flush()
                record_audit(
                    db,
                    action_type="pursuit_created",
                    user_id=actor.id,
                    opportunity_id=opp_id,
                    entity_type="pursuits",
                    entity_id=pursuit.id,
                    new_value={"stage": "evaluating"},
                )
    except PermissionDenied as exc:
        return _redirect(f"/opp/{opp_id}", error=_error_text(exc))
    return RedirectResponse(f"/workspace/{opp_id}", status_code=303)


# ── Workspace ─────────────────────────────────────────────────────────────────


def workspace(request: Request, opp_id: int) -> HTMLResponse:
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)

    active_tab = request.query_params.get("tab", "overview")

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
            select(AIAnalysis).where(AIAnalysis.opportunity_id == opp_id, AIAnalysis.analysis_type == AnalysisType.SOLICITATION_SUMMARY)
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
            from govcon.compliance.matrix import latest_run
            from govcon.compliance.submission_preflight import readiness_blockers

            tab_ctx["compliance_run"] = latest_run(db, opp_id, "compliance_matrix")
            tab_ctx["preflight_run"] = latest_run(db, opp_id, "submission_preflight")
            tab_ctx["preflight_blockers"] = readiness_blockers(db, opp_id) if tab_ctx["preflight_run"] else []

        if active_tab == "awards" or active_tab == "market":
            tab_ctx["awards"] = db.scalars(
                select(Award).where(
                    (Award.nsn == opp.nsn) if opp.nsn else (Award.psc_code == opp.psc_code)
                ).order_by(desc(Award.action_date)).limit(50)
            ).all() if (opp.nsn or opp.psc_code) else []

        if active_tab == "market":
            tab_ctx["ai_market"] = db.scalar(
                select(AIAnalysis).where(AIAnalysis.opportunity_id == opp_id, AIAnalysis.analysis_type == AnalysisType.MARKET)
                .order_by(desc(AIAnalysis.created_at))
            )

        if active_tab == "products":
            tab_ctx["ai_sourcing"] = db.scalar(
                select(AIAnalysis).where(AIAnalysis.opportunity_id == opp_id, AIAnalysis.analysis_type == AnalysisType.SOURCING)
                .order_by(desc(AIAnalysis.created_at))
            )

        if active_tab == "pricing":
            tab_ctx["ai_pricing"] = db.scalar(
                select(AIAnalysis).where(AIAnalysis.opportunity_id == opp_id, AIAnalysis.analysis_type == AnalysisType.PRICING)
                .order_by(desc(AIAnalysis.created_at))
            )

        if active_tab == "competitors":
            tab_ctx["competitors"] = _get_competitor_detail(db, opp)

        if active_tab == "ai_decision":
            tab_ctx["decision_package_analysis"] = db.scalar(
                select(AIAnalysis).where(AIAnalysis.opportunity_id == opp_id, AIAnalysis.analysis_type == AnalysisType.DECISION_PACKAGE)
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
            tab_ctx["approval"] = approval_context(db, opportunity_id=opp_id) if review_session else None
            tab_ctx["assignable_users"] = [
                u
                for u in db.scalars(
                    select(User).where(User.is_active.is_(True)).order_by(User.display_name)
                ).all()
                if u.role in ("owner", "approver", "reviewer")
            ]
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
            tab_ctx["approvable_statuses"] = sorted(APPROVABLE_PROPOSAL_STATUSES)
            if proposal is not None:
                tab_ctx["proposal_workspace"] = get_proposal_workspace(db, opportunity_id=opp_id)

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
            tab_ctx["can_approve"] = user.role in ("owner", "approver")
            if sub is not None:
                from govcon.submissions.checklist import generate_final_checklist, generate_step_by_step_instructions

                tab_ctx["submission_checklist"] = generate_final_checklist(db, opportunity_id=opp_id)
                tab_ctx["submission_instructions"] = generate_step_by_step_instructions(db, opportunity_id=opp_id)

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
            "primary_source_url": primary_source_url(opp.links),
            "can_run_analysis": user.role in ("owner", "approver", "reviewer"),
            "deadline_label": deadline_label,
            "deadline_class": deadline_cls,
            "pursuit": pursuit,
            "review_session": review_session,
            "bid_decision": bid_decision,
            "ai_summary": ai_summary,
            "active_tab": active_tab,
            "active_page": "pipeline",
            **tab_ctx,
        }
        return _render(request, "workspace.html", ctx, user)


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
    body: Annotated[str, Form()] = "",
    user_recommendation: Annotated[str | None, Form()] = None,
    topic: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    """Add a comment through the comment service (assignment, AI side-opinion, audit, notifications)."""
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    target = f"/workspace/{opp_id}?tab=review"
    try:
        with session_scope() as db:
            actor = _actor(db, user, "review")
            add_comment(
                db,
                opportunity_id=opp_id,
                user_id=actor.id,
                body=body,
                topic=(topic or "").strip() or None,
                user_recommendation=user_recommendation or None,
                validate_with_ai=True,
            )
    except _WORKFLOW_ERRORS as exc:
        return _redirect(target, error=_error_text(exc))
    return _redirect(target, notice="Comment added.")


_ANALYSIS_TABS = {"market": "market", "supplier": "products", "pricing": "pricing"}


def workspace_run_analysis(request: Request, opp_id: int, kind: str) -> HTMLResponse:
    """Run the market / supplier / pricing AI analysis from its workspace tab."""
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    from govcon.ai.structured import StructuredCallError
    from govcon.intelligence.ai_analyses import PRODUCERS

    tab = _ANALYSIS_TABS.get(kind)
    if tab is None or kind not in PRODUCERS:
        return HTMLResponse("Unknown analysis", status_code=404)
    target = f"/workspace/{opp_id}?tab={tab}"
    try:
        with session_scope() as db:
            actor = _actor(db, user, "review")
            analysis = PRODUCERS[kind](db, opp_id)
            record_audit(
                db,
                action_type="ai_analysis_requested",
                user_id=actor.id,
                opportunity_id=opp_id,
                entity_type="ai_analyses",
                entity_id=analysis.id,
                new_value={"kind": kind},
            )
    except StructuredCallError as exc:
        return _redirect(target, error=f"Analysis not run ({exc.reason}): {exc.detail}")
    except _WORKFLOW_ERRORS as exc:
        return _redirect(target, error=_error_text(exc))
    return _redirect(target, notice=f"{kind.title()} analysis complete.")


def workspace_assign_reviewer(
    request: Request,
    opp_id: int,
    user_id: Annotated[int, Form()],
    assignment_role: Annotated[str, Form()] = "reviewer",
) -> HTMLResponse:
    """Assign a reviewer (owner/approver only)."""
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    target = f"/workspace/{opp_id}?tab=review"
    try:
        with session_scope() as db:
            actor = _actor(db, user, "approve")
            if db.get(Opportunity, opp_id) is None:
                return HTMLResponse("Opportunity not found", status_code=404)
            reviewer = db.get(User, user_id)
            if reviewer is None or not reviewer.is_active:
                raise ValueError("reviewer must be an active user")
            require_permission(reviewer, "review")
            ensure_review_session(db, opportunity_id=opp_id)
            assign_reviewer(
                db,
                opportunity_id=opp_id,
                user_id=reviewer.id,
                assignment_role=assignment_role or "reviewer",
                actor_user_id=actor.id,
            )
            recalculate_quorum(db, opportunity_id=opp_id)
    except _WORKFLOW_ERRORS as exc:
        return _redirect(target, error=_error_text(exc))
    return _redirect(target, notice="Reviewer assigned.")


def workspace_complete_review(
    request: Request,
    opp_id: int,
    recommendation: Annotated[str | None, Form()] = None,
    action: Annotated[str, Form()] = "approve_continue",
    agree_with_ai_assessment: Annotated[str | None, Form()] = None,
    second_review_reason: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    """Complete the caller's own review assignment through the quorum service."""
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    target = f"/workspace/{opp_id}?tab=review"
    agree = True if (agree_with_ai_assessment or "").lower() in {"on", "true", "1", "yes"} else None
    try:
        with session_scope() as db:
            actor = _actor(db, user, "review")
            complete_assignment(
                db,
                opportunity_id=opp_id,
                user_id=actor.id,
                action=action,
                recommendation=recommendation or None,
                agree_with_ai_assessment=agree,
                second_review_reason=second_review_reason,
            )
    except _WORKFLOW_ERRORS as exc:
        return _redirect(target, error=_error_text(exc))
    return _redirect(target, notice="Review recorded.")


def workspace_approve(
    request: Request,
    opp_id: int,
    decision: Annotated[str, Form()],
    expected_version: Annotated[str | None, Form()] = None,
    override_reason: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    """Final bid decision through ``finalize_approval`` (quorum, override, version, audit)."""
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    target = f"/workspace/{opp_id}?tab=review"
    version = _form_int(expected_version)
    if version is None:
        return _redirect(target, error="The review changed or the form is out of date; reload and try again.")
    try:
        with session_scope() as db:
            actor = _actor(db, user, "approve")
            finalize_approval(
                db,
                opportunity_id=opp_id,
                actor=actor,
                action=decision,
                expected_version=version,
                override_reason=(override_reason or "").strip() or None,
            )
            if decision == "approve_to_bid":
                _trigger_proposal_generation(db, opp_id=opp_id, actor=actor)
    except _WORKFLOW_ERRORS as exc:
        return _redirect(target, error=_error_text(exc))
    return _redirect(target, notice=f"Decision recorded: {decision.replace('_', ' ')}.")


def _trigger_proposal_generation(db: Any, *, opp_id: int, actor: Any) -> None:
    """Call Phase 11 generate_proposal + generate_submission_package.

    Runs in a savepoint: a generation failure is logged and rolled back without
    undoing the approval itself. ``skip_ai`` is used when no AI provider key is
    configured, so the UI shows a real draft with [[BLOCKER:...]] markers.
    """
    import logging
    _log = logging.getLogger("govcon.web.routes")
    from govcon.db import current_settings as get_settings
    from govcon.proposals.service import generate_proposal
    from govcon.submissions.service import generate_submission_package

    settings = get_settings()
    from govcon.ai.providers import provider_available
    skip = not provider_available(settings)
    try:
        with db.begin_nested():
            generate_proposal(db, opportunity_id=opp_id, actor=actor, skip_ai=skip)
            generate_submission_package(db, opportunity_id=opp_id, actor=actor)
    except Exception as exc:  # generation must not undo a recorded human decision
        _log.warning("auto-generate on approve_to_bid failed for opp %s: %s", opp_id, exc)


def workspace_proposal_approve(
    request: Request,
    opp_id: int,
    decision: Annotated[str, Form()],
    expected_version: Annotated[str | None, Form()] = None,
    override_reason: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    """Final proposal decision through ``finalize_proposal`` and the compliance gate."""
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    target = f"/workspace/{opp_id}?tab=proposal"
    version = _form_int(expected_version)
    if version is None:
        return _redirect(target, error="The proposal changed or the form is out of date; reload and try again.")
    try:
        with session_scope() as db:
            actor = _actor(db, user, "approve")
            finalize_proposal(
                db,
                opportunity_id=opp_id,
                action=decision,
                actor=actor,
                expected_version=version,
                override_reason=(override_reason or "").strip() or None,
            )
    except _WORKFLOW_ERRORS as exc:
        return _redirect(target, error=_error_text(exc))
    return _redirect(target, notice=f"Proposal decision recorded: {decision}.")


def workspace_submission_approve(
    request: Request,
    opp_id: int,
    expected_version: Annotated[str | None, Form()] = None,
    confirmation_number: Annotated[str | None, Form()] = None,
    confirmation_notes: Annotated[str | None, Form()] = None,
    submitted_at: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    """Record the human's manual submission with confirmation evidence."""
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    target = f"/workspace/{opp_id}?tab=submission"
    version = _form_int(expected_version)
    if version is None:
        return _redirect(target, error="The submission changed or the form is out of date; reload and try again.")
    when: datetime | None = None
    if submitted_at and submitted_at.strip():
        try:
            when = datetime.fromisoformat(submitted_at.strip())
        except ValueError:
            return _redirect(target, error="Submission time is not a valid date/time.")
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
    try:
        with session_scope() as db:
            actor = _actor(db, user, "approve")
            record_submission_confirmation(
                db,
                opportunity_id=opp_id,
                actor=actor,
                expected_version=version,
                confirmation_number=confirmation_number,
                confirmation_notes=confirmation_notes,
                submitted_at=when,
            )
    except _WORKFLOW_ERRORS as exc:
        return _redirect(target, error=_error_text(exc))
    return _redirect(target, notice="Submission recorded.")


async def workspace_record_outcome(
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
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    target = f"/workspace/{opp_id}?tab=submission"

    valid_outcomes = ("won", "lost", "no_bid", "cancelled")
    if outcome not in valid_outcomes:
        return _redirect(target, error=f"Unknown outcome {outcome!r}.")

    def _float(val: str | None, field: str) -> float | None:
        import math
        if val is None or not val.strip():
            return None
        try:
            value = float(val)
        except ValueError as exc:
            raise ValueError(f"{field} must be a valid number") from exc
        if not math.isfinite(value) or (field != "win_margin_pct" and value < 0):
            raise ValueError(f"{field} must be finite" + (" and nonnegative" if field != "win_margin_pct" else ""))
        return value

    try:
        form = await request.form()
        for field in ("award_amount", "known_winning_price", "win_margin_pct"):
            if len(form.getlist(field)) > 1:
                raise ValueError(f"duplicate {field} fields are not allowed")
        amount = _float(award_amount, "award_amount")
        winning_price = _float(known_winning_price, "known_winning_price")
        margin = _float(win_margin_pct, "win_margin_pct")
        with session_scope() as db:
            actor = _actor(db, user, "approve")
            record_outcome(
                db,
                opportunity_id=opp_id,
                outcome=outcome,
                actor=actor,
                no_bid_reason=no_bid_reason or None,
                no_bid_category=no_bid_category or None,
                loss_reason=loss_reason or None,
                known_winning_price=winning_price,
                win_reason=win_reason or None,
                win_margin_pct=margin,
                win_supplier=win_supplier or None,
                win_delivery_terms=win_delivery_terms or None,
                win_proposal_version=win_proposal_version or None,
                awarded_vendor_name=awarded_vendor_name or None,
                awarded_vendor_uei=awarded_vendor_uei or None,
                award_amount=amount,
                government_feedback=government_feedback or None,
                debrief_notes=debrief_notes or None,
                lessons_learned=lessons_learned or None,
            )
    except _WORKFLOW_ERRORS as exc:
        return _redirect(target, error=_error_text(exc))
    return _redirect(target, notice=f"Outcome recorded: {outcome}.")


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
    ("cancelled",           "Cancelled"),
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
            .where(Match.status.in_(["new", "seen", "reviewing"]), Match.active.is_(True))
            .where(~Match.opportunity_id.in_(list(matched_opp_ids) or [-1]))
            .limit(50)
        ).all()

        stage_cards: dict[str, list[dict]] = {k: [] for k, _ in _PIPELINE_COLUMNS}

        for p, opp in pursuits:
            stage = p.stage or "evaluating"
            # Map review session statuses to pipeline stage
            rs = db.scalar(select(ReviewSession).where(ReviewSession.opportunity_id == opp.id))
            if rs and stage in {"evaluating", "sourcing", "bid_approved"}:
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
    try:
        require_permission(user, "manage_watchlists")
    except PermissionDenied as exc:
        return _redirect("/watchlists", error=_error_text(exc))
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
    try:
        require_permission(user, "manage_watchlists")
    except PermissionDenied as exc:
        return _redirect("/watchlists", error=_error_text(exc))
    return await _watchlist_save(request, user, wl_id=wl_id)


async def _watchlist_save(request: Request, user: User, wl_id: int | None) -> HTMLResponse:
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        ctx = {"editing": wl_id is not None, "wl": None, "error": "Name is required.", "active_page": "watchlists"}
        return _render(request, "watchlist_edit.html", ctx, user)

    def parse_list(val: str | None) -> list[str] | None:
        if not val or not val.strip():
            return None
        return [x.strip() for x in val.split(",") if x.strip()]

    def parse_num(val: str | None):
        try:
            return float(val) if val and val.strip() else None
        except ValueError:
            return None

    with session_scope() as db:
        actor = _actor(db, user, "manage_watchlists")
        if wl_id:
            wl = db.get(Watchlist, wl_id)
            if wl is None:
                return HTMLResponse("Not found", status_code=404)
            old_value = {
                "name": wl.name,
                "psc_codes": wl.psc_codes,
                "naics_codes": wl.naics_codes,
                "keywords": wl.keywords,
                "exclude_keywords": wl.exclude_keywords,
                "nsn_list": wl.nsn_list,
                "set_asides": wl.set_asides,
                "sources": wl.sources,
            }
        else:
            wl = Watchlist()
            db.add(wl)
            old_value = None

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
        record_audit(
            db,
            action_type="watchlist_updated" if wl_id else "watchlist_created",
            user_id=actor.id,
            entity_type="watchlists",
            entity_id=wl.id,
            old_value=old_value,
            new_value={
                "name": wl.name,
                "psc_codes": wl.psc_codes,
                "naics_codes": wl.naics_codes,
                "keywords": wl.keywords,
                "exclude_keywords": wl.exclude_keywords,
                "nsn_list": wl.nsn_list,
                "set_asides": wl.set_asides,
                "sources": wl.sources,
            },
        )

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

    try:
        with session_scope() as db:
            actor = _actor(db, user, "manage_watchlists")
            wl = db.get(Watchlist, wl_id)
            if wl:
                old = wl.enabled
                wl.enabled = enabled.lower() == "true"
                record_audit(
                    db,
                    action_type="watchlist_toggled",
                    user_id=actor.id,
                    entity_type="watchlists",
                    entity_id=wl.id,
                    old_value={"enabled": old},
                    new_value={"enabled": wl.enabled},
                )
    except PermissionDenied as exc:
        return _redirect("/watchlists", error=_error_text(exc))
    return RedirectResponse("/watchlists", status_code=303)


def watchlist_rebuild(request: Request, wl_id: int) -> HTMLResponse:
    """Re-evaluate one watchlist and drop matches it no longer produces."""
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)

    from govcon.matching.engine import run_matching

    try:
        with session_scope() as db:
            actor = _actor(db, user, "manage_watchlists")
            stats = run_matching(db, watchlist_id=wl_id, rebuild=True)
            record_audit(
                db,
                action_type="watchlist_rebuilt",
                user_id=actor.id,
                entity_type="watchlists",
                entity_id=wl_id,
                new_value={
                    "matched": stats.matched,
                    "inserted": stats.inserted,
                    "updated": stats.updated,
                    "removed": stats.removed,
                },
            )
    except _WORKFLOW_ERRORS as exc:
        return _redirect("/watchlists", error=f"Rebuild failed: {_error_text(exc)}")
    return _redirect(
        "/watchlists",
        notice=f"Rebuilt: {stats.matched} matched, {stats.inserted} new, {stats.removed} removed.",
    )


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
        match_count = db.scalar(select(func.count()).select_from(Match).where(Match.active.is_(True))) or 0
        match_new = db.scalar(
            select(func.count()).select_from(Match).where(Match.status == "new", Match.active.is_(True))
        ) or 0
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
