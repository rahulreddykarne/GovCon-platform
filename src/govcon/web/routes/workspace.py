"""Workspace endpoints and supporting views."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session as OrmSession

from govcon.ai.analysis_types import AnalysisType
from govcon.audit import record_audit
from govcon.collaboration.comments import list_comments
from govcon.collaboration.review_sessions import approval_context
from govcon.collaboration.users import PermissionDenied, can
from govcon.db import session_scope
from govcon.intelligence.analysis_tasks import latest_analysis_tasks
from govcon.learning.outcomes import NO_BID_CATEGORIES
from govcon.models import (
    AIAnalysis,
    AuditEvent,
    Award,
    BidDecision,
    Contact,
    Opportunity,
    OpportunityEvent,
    Proposal,
    ProposalVersion,
    Pursuit,
    Requirement,
    ReviewAssignment,
    ReviewSession,
    StoredFile,
    Submission,
    Task,
    User,
)
from govcon.proposals.service import get_proposal_workspace
from govcon.tasks.queue import latest_task
from govcon.web.helpers import deadline_info, primary_source_url, source_links
from govcon.web.presentation import ViewRow
from govcon.web.routes.common import (
    _WORKFLOW_ERRORS,
    _actor,
    _error_text,
    _NeedsLogin,
    _redirect,
    _render,
    _require_login,
)
from govcon.web.routes.proposals import _generation_status
from govcon.web.routes.sourcing import _sourcing_context
from govcon.workflow.invalidation import lock_opportunity
from govcon.workflow.preparation import PREPARATION_TASK, preparation_view
from govcon.workflow.proposal_generation import artifacts_exist
from govcon.workflow.transitions import (
    APPROVABLE_PROPOSAL_STATUSES,
    TERMINAL_PURSUIT_STAGES,
)


def opp_detail(request: Request, opp_id: int) -> Response:
    # Legacy URLs share the canonical workspace view; pursuing remains an explicit POST.
    return workspace(request, opp_id)


def opp_start_workspace(request: Request, opp_id: int) -> Response:
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
            from govcon.workflow.pursuits import create_or_get_pursuit

            create_or_get_pursuit(db, opportunity_id=opp_id, actor=actor, origin="web_workspace")
    except PermissionDenied as exc:
        return _redirect(f"/opp/{opp_id}", error=_error_text(exc), request=request)
    return RedirectResponse(f"/workspace/{opp_id}", status_code=303)


WORKSPACE_TABS = [
    ("overview", "Overview"), ("compliance", "Requirements & Compliance"), ("market", "Market"),
    ("sourcing", "Sourcing & Pricing"), ("review", "Review & Decision"),
    ("submission", "Proposal & Submission"), ("activity", "Activity"),
]


def workspace(request: Request, opp_id: int, requirements_page: Annotated[int, Query(ge=1, le=1_000_000)] = 1) -> Response:
    from govcon.web.progress import progress_state
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)

    aliases = {"requirements": "compliance", "awards": "market", "competitors": "market",
               "products": "sourcing", "pricing": "sourcing", "ai_decision": "review", "proposal": "submission"}
    requested_tab = request.query_params.get("tab", "overview")
    active_tab = aliases.get(requested_tab, requested_tab)
    if active_tab not in {"overview", "compliance", "market", "sourcing", "review", "submission", "activity"}:
        active_tab = "overview"

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

        if active_tab == "overview":
            tab_ctx["attachments"] = db.scalars(select(StoredFile).where(StoredFile.opportunity_id == opp_id)).all()
            tab_ctx["events"] = db.scalars(select(OpportunityEvent).where(OpportunityEvent.opportunity_id == opp_id)
                .order_by(OpportunityEvent.detected_at.desc()).limit(20)).all()
            tab_ctx["contacts"] = db.scalars(select(Contact).where(Contact.agency_path == opp.agency_path).limit(5)).all() if opp.agency_path else []
            tab_ctx["source_links"] = source_links(opp.links)

        if active_tab == "requirements" or active_tab == "compliance":
            total = db.scalar(select(func.count()).select_from(Requirement).where(Requirement.opportunity_id == opp_id)) or 0
            page_size = 200
            pages = max(1, (total + page_size - 1) // page_size)
            page = min(requirements_page, pages)
            tab_ctx.update(requirements_total=total, requirements_page=page, requirements_pages=pages,
                           requirements_start=(page - 1) * page_size + 1 if total else 0,
                           requirements_end=min(total, page * page_size))
            tab_ctx["requirements"] = db.scalars(
                select(Requirement).where(Requirement.opportunity_id == opp_id)
                .order_by(Requirement.severity.nullslast(), Requirement.id)
                .offset((page - 1) * page_size).limit(page_size)
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

        if active_tab == "sourcing":
            tab_ctx.update(_sourcing_context(db, opp_id))
            tab_ctx["ai_sourcing"] = db.scalar(
                select(AIAnalysis).where(AIAnalysis.opportunity_id == opp_id, AIAnalysis.analysis_type == AnalysisType.SOURCING)
                .order_by(desc(AIAnalysis.created_at))
            )

        if active_tab == "sourcing":
            tab_ctx["ai_pricing"] = db.scalar(
                select(AIAnalysis).where(AIAnalysis.opportunity_id == opp_id, AIAnalysis.analysis_type == AnalysisType.PRICING)
                .order_by(desc(AIAnalysis.created_at))
            )

        if active_tab == "market":
            tab_ctx["competitors"] = _get_competitor_detail(db, opp)

        if active_tab in {"review", "overview"}:
            tab_ctx["decision_package_analysis"] = db.scalar(
                select(AIAnalysis).where(AIAnalysis.opportunity_id == opp_id, AIAnalysis.analysis_type == AnalysisType.DECISION_PACKAGE)
                .order_by(desc(AIAnalysis.created_at))
            )

        if active_tab == "review":
            tab_ctx["no_bid_categories"] = sorted(NO_BID_CATEGORIES)
            tab_ctx["assignments"] = _get_assignments_with_names(db, opp_id)
            comments_raw = list_comments(db, opportunity_id=opp_id)
            tab_ctx["comment_tasks"] = {}
            for task in db.scalars(select(Task).where(
                Task.opportunity_id == opp_id, Task.task_type == "ai_analysis",
                Task.payload["kind"].astext == "reviewer_comment_validation",
            ).order_by(Task.id.desc())):
                tab_ctx["comment_tasks"].setdefault((task.payload or {}).get("comment_id"), task)
            user_map = _user_display_map(db)
            tab_ctx["comments"] = [
                ViewRow({
                    **{k: getattr(c, k) for k in c.__class__.__table__.columns.keys()},
                    "user_display_name": user_map.get(c.user_id, "?"),
                })
                for c in comments_raw
            ]
            tab_ctx["can_approve"] = can(user, "approve")
            tab_ctx["can_review"] = can(user, "review")
            tab_ctx["approval"] = approval_context(db, opportunity_id=opp_id) if review_session else None
            tab_ctx["assignable_users"] = [
                u
                for u in db.scalars(
                    select(User).where(User.is_active.is_(True)).order_by(User.display_name)
                ).all()
                if can(u, "review")
            ]
            my_assign = db.scalar(
                select(ReviewAssignment).where(
                    ReviewAssignment.opportunity_id == opp_id,
                    ReviewAssignment.user_id == user.id,
                )
            )
            tab_ctx["my_assignment"] = my_assign
            tab_ctx["can_comment"] = tab_ctx["can_review"] and my_assign is not None

        if active_tab == "submission":
            proposal = db.scalar(select(Proposal).where(Proposal.opportunity_id == opp_id))
            tab_ctx["proposal"] = proposal
            if proposal and proposal.current_version_id:
                tab_ctx["proposal_version"] = db.get(ProposalVersion, proposal.current_version_id)
                from govcon.proposals.versions import get_sections_for_version

                tab_ctx["proposal_sections"] = get_sections_for_version(db, proposal.current_version_id)
            tab_ctx["can_edit_proposal"] = can(user, "review")
            tab_ctx["can_approve"] = can(user, "approve")
            tab_ctx["approvable_statuses"] = sorted(APPROVABLE_PROPOSAL_STATUSES)
            if proposal is not None:
                tab_ctx["proposal_workspace"] = get_proposal_workspace(db, opportunity_id=opp_id)
            approved = review_session is not None and review_session.status == "approved_to_bid"
            generation = _generation_status(db, opp_id) if approved else {"task": None, "active": False}
            tab_ctx["generation_task"] = generation["task"]
            tab_ctx["generation_active"] = generation["active"]
            tab_ctx["can_retry_generation"] = (
                approved
                and tab_ctx["can_approve"]
                and not generation["active"]
                and not artifacts_exist(db, opp_id)
            )

        if active_tab == "submission":
            from govcon.models import OutcomeSuggestion
            from govcon.workflow.transitions import can_transition

            tab_ctx["outcome_options"] = [
                (value, label) for value, label in (
                    ("won", "Won"), ("lost", "Lost"), ("cancelled", "Cancelled / No Award")
                ) if pursuit and pursuit.stage not in ("won", "lost", "no_bid", "cancelled")
                and can_transition("pursuit", pursuit.stage, value)
            ]

            tab_ctx["outcome_suggestions"] = list(db.scalars(
                select(OutcomeSuggestion).where(OutcomeSuggestion.opportunity_id == opp_id)
                .order_by(OutcomeSuggestion.strength.desc(), OutcomeSuggestion.id.desc())
            ).all())
            proposal = db.scalar(select(Proposal).where(Proposal.opportunity_id == opp_id))
            tab_ctx["proposal"] = proposal
            version = db.get(ProposalVersion, proposal.current_version_id) if proposal and proposal.current_version_id else None
            tab_ctx["outcome_proposal_version"] = version.version_number if version else None
            # Load submission by opportunity_id (Phase 11 generates it that way)
            sub = db.scalar(
                select(Submission).where(Submission.opportunity_id == opp_id)
                .order_by(desc(Submission.created_at))
            )
            # Also check proposal.submission_id as fallback
            if sub is None and proposal and proposal.submission_id:
                sub = db.get(Submission, proposal.submission_id)
            tab_ctx["submission"] = sub
            tab_ctx["can_approve"] = can(user, "approve")
            if sub is not None:
                from govcon.submissions.checklist import (
                    generate_final_checklist,
                    generate_step_by_step_instructions,
                )

                tab_ctx["submission_checklist"] = generate_final_checklist(db, opportunity_id=opp_id)
                tab_ctx["submission_instructions"] = generate_step_by_step_instructions(db, opportunity_id=opp_id)

        if active_tab == "activity":
            evts = db.execute(
                select(AuditEvent).where(AuditEvent.opportunity_id == opp_id)
                .order_by(desc(AuditEvent.created_at)).limit(100)
            ).scalars().all()
            user_map = _user_display_map(db)
            tab_ctx["audit_events"] = [
                ViewRow({
                    **{k: getattr(e, k) for k in e.__class__.__table__.columns.keys()},
                    "user_display_name": user_map.get(e.user_id, "system") if e.user_id else "system",
                })
                for e in evts
            ]

        ctx: dict[str, Any] = {
            "opp": opp,
            "primary_source_url": primary_source_url(opp.links),
            "can_run_analysis": can(user, "review") and (not pursuit or pursuit.stage not in TERMINAL_PURSUIT_STAGES),
            "analysis_tasks": latest_analysis_tasks(db, opp_id),
            "preparation": preparation_view(latest_task(db, task_type=PREPARATION_TASK, opportunity_id=opp_id)),
            "deadline_label": deadline_label,
            "deadline_class": deadline_cls,
            "pursuit": pursuit,
            "review_session": review_session,
            "bid_decision": bid_decision,
            "ai_summary": ai_summary,
            "active_tab": active_tab,
            "active_page": "pipeline",
            "workspace_tabs": WORKSPACE_TABS,
            "progress": progress_state(db, opp_id),
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
        ViewRow({
            "recipient_uei": r.recipient_uei,
            "recipient_name": r.recipient_name,
            "award_count": r.award_count,
            "total_value": r.total_value,
        })
        for r in rows
    ]


def _get_assignments_with_names(db: OrmSession, opp_id: int) -> list[Any]:
    rows = db.execute(
        select(ReviewAssignment, User)
        .join(User, ReviewAssignment.user_id == User.id)
        .where(ReviewAssignment.opportunity_id == opp_id)
    ).all()
    return [
        ViewRow({
            **{k: getattr(a, k) for k in a.__class__.__table__.columns.keys()},
            "user_display_name": u.display_name,
        })
        for a, u in rows
    ]


def _user_display_map(db: OrmSession) -> dict[int, str]:
    rows = db.execute(select(User.id, User.display_name)).all()
    return {r.id: r.display_name for r in rows}


def workspace_prepare(request: Request, opp_id: int) -> Response:
    """Queue (or re-run) automatic preparation for a pursued opportunity."""
    from govcon.workflow.preparation import queue_preparation

    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    target = f"/workspace/{opp_id}?tab=overview"
    try:
        with session_scope() as db:
            actor = _actor(db, user, "review")
            if lock_opportunity(db, opp_id) is None:
                raise ValueError("opportunity not found")
            pursuit = db.scalar(select(Pursuit).where(Pursuit.opportunity_id == opp_id))
            if pursuit is None:
                raise ValueError("start a pursuit before preparing the opportunity")
            if pursuit.stage in TERMINAL_PURSUIT_STAGES:
                raise ValueError("A closed pursuit cannot be prepared. Reopen the review before preparing a no-bid decision.")
            task, created = queue_preparation(db, opportunity_id=opp_id, actor_user_id=actor.id)
            record_audit(db, action_type="preparation_requested", user_id=actor.id, opportunity_id=opp_id,
                         entity_type="tasks", entity_id=task.id, new_value={"queued": created})
    except _WORKFLOW_ERRORS as exc:
        return _redirect(target, error=_error_text(exc), request=request)
    return _redirect(target, notice="Preparation queued." if created else "Preparation is already queued or running.", request=request)
