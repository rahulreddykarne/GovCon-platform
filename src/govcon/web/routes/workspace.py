"""Workspace endpoints and supporting views."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session as OrmSession

from govcon.ai.analysis_types import AnalysisType
from govcon.ai.budget import opportunity_budget_status
from govcon.audit import record_audit
from govcon.collaboration.comments import list_comments
from govcon.collaboration.review_sessions import approval_context
from govcon.collaboration.users import PermissionDenied, can
from govcon.config import get_settings
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
    _form_int,
    _NeedsLogin,
    _redirect,
    _render,
    _require_login,
)
from govcon.web.routes.proposals import _generation_status
from govcon.web.routes.sourcing import _sourcing_context
from govcon.workflow.attachment_download import document_ready_notice
from govcon.workflow.invalidation import lock_opportunity
from govcon.workflow.preparation import PREPARATION_TASK, REVIEW_TASK, preparation_view
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
    except _WORKFLOW_ERRORS as exc:
        # e.g. an award notice or a closed solicitation: shown, not a server error.
        return _redirect(f"/workspace/{opp_id}", error=_error_text(exc), request=request)
    return RedirectResponse(f"/workspace/{opp_id}", status_code=303)


WORKSPACE_TABS = [
    ("summary", "Summary"),
    ("compliance", "Compliance"),
    ("decision", "Decision"),
    ("pricing", "Pricing / Products"),
    ("documents", "Documents"),
]
WORKSPACE_MORE_TABS = [
    ("market", "Market"),
    ("submission", "Proposal"),
    ("activity", "Activity"),
]
WORKSPACE_TAB_KEYS = {key for key, _label in WORKSPACE_TABS + WORKSPACE_MORE_TABS}


REQUIREMENTS_PAGE_SIZE = 25
OVERRIDE_STATUS_LABELS = (
    ("satisfied", "Met"), ("missing", "Missing"), ("needs_review", "Needs review"), ("not_applicable", "Not applicable"),
)


def workspace(request: Request, opp_id: int, requirements_page: Annotated[int, Query(ge=1, le=1_000_000)] = 1) -> Response:
    from govcon.web.progress import progress_state
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)

    aliases = {
        "overview": "summary", "requirements": "compliance", "review": "decision", "ai_decision": "decision",
        "sourcing": "pricing", "products": "pricing", "awards": "market", "competitors": "market",
        "proposal": "submission",
    }
    requested_tab = request.query_params.get("tab", "summary")
    active_tab = aliases.get(requested_tab, requested_tab)
    if active_tab not in WORKSPACE_TAB_KEYS:
        active_tab = "summary"

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
        from govcon.enrich.summarize import latest_solicitation_summary

        ai_summary = latest_solicitation_summary(db, opp_id)

        # build tab-specific context
        tab_ctx: dict[str, Any] = {}

        if active_tab in {"summary", "documents"}:
            tab_ctx["attachments"] = db.scalars(select(StoredFile).where(StoredFile.opportunity_id == opp_id)).all()
        if active_tab == "summary":
            tab_ctx["events"] = db.scalars(select(OpportunityEvent).where(OpportunityEvent.opportunity_id == opp_id)
                .order_by(OpportunityEvent.detected_at.desc()).limit(20)).all()
            tab_ctx["contacts"] = db.scalars(select(Contact).where(Contact.agency_path == opp.agency_path).limit(5)).all() if opp.agency_path else []
            tab_ctx["source_links"] = source_links(opp.links)
            from govcon.operating.trace import opportunity_trace

            tab_ctx["stage_trace"] = opportunity_trace(db, opp)

        if active_tab == "compliance":
            from govcon.compliance.matrix import OVERRIDE_STATUSES, latest_run
            from govcon.compliance.pipeline import (
                CompanyFactsInvalid,
                load_company_facts,
            )
            from govcon.compliance.submission_preflight import readiness_blockers
            from govcon.compliance.view import compliance_view

            try:
                facts = load_company_facts(get_settings(), db)
                tab_ctx["company_facts_error"] = None
            except CompanyFactsInvalid as exc:
                facts = {}
                tab_ctx["company_facts_error"] = str(exc)
            view = compliance_view(db, opp_id, facts=facts)
            total = len(view.rows)
            page_size = REQUIREMENTS_PAGE_SIZE
            pages = max(1, (total + page_size - 1) // page_size)
            page = min(requirements_page, pages)
            tab_ctx.update(requirements_total=total, requirements_page=page, requirements_pages=pages,
                           requirements_start=(page - 1) * page_size + 1 if total else 0,
                           requirements_end=min(total, page * page_size))
            tab_ctx["compliance"] = view
            page_rows = view.rows[(page - 1) * page_size: page * page_size]
            tab_ctx["requirement_rows"] = page_rows
            sections: list[dict[str, Any]] = []
            current: dict[str, Any] | None = None
            for row in page_rows:
                if current is None or current["category"] != row["category"]:
                    current = {"category": row["category"], "label": row["category_label"], "rows": []}
                    sections.append(current)
                current["rows"].append(row)
            tab_ctx["requirement_sections"] = sections
            tab_ctx["compliance_run"] = view.run
            tab_ctx["can_override"] = can(user, "override_compliance")
            tab_ctx["override_statuses"] = [(s, label) for s, label in OVERRIDE_STATUS_LABELS if s in OVERRIDE_STATUSES]
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

        if active_tab == "pricing":
            tab_ctx.update(_sourcing_context(db, opp_id))
            tab_ctx["ai_sourcing"] = db.scalar(
                select(AIAnalysis).where(AIAnalysis.opportunity_id == opp_id, AIAnalysis.analysis_type == AnalysisType.SOURCING)
                .order_by(desc(AIAnalysis.created_at))
            )

        if active_tab == "pricing":
            tab_ctx["ai_pricing"] = db.scalar(
                select(AIAnalysis).where(AIAnalysis.opportunity_id == opp_id, AIAnalysis.analysis_type == AnalysisType.PRICING)
                .order_by(desc(AIAnalysis.created_at))
            )

        if active_tab == "market":
            tab_ctx["competitors"] = _get_competitor_detail(db, opp)

        if active_tab in {"decision", "summary"}:
            tab_ctx["decision_package_analysis"] = db.scalar(
                select(AIAnalysis).where(AIAnalysis.opportunity_id == opp_id, AIAnalysis.analysis_type == AnalysisType.DECISION_PACKAGE)
                .order_by(desc(AIAnalysis.created_at))
            )

        if active_tab == "decision":
            from govcon.decision.engine import build_decision_state
            from govcon.decision.scorecard import build_scorecard

            stored = (bid_decision.rules_result or {}) if bid_decision is not None else {}
            live = build_scorecard(build_decision_state(db, opp_id)).as_dict()
            tab_ctx["scorecard"] = stored.get("scorecard") or live
            tab_ctx["arbitration"] = stored.get("arbitration")
            tab_ctx["decision_needs_information"] = stored.get("needs_information") or []
            tab_ctx["decision_blockers"] = stored.get("hard_rule_blockers") or []
            tab_ctx["estimated_value_note"] = (tab_ctx["scorecard"] or {}).get("estimated_value_note")
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
            "review_analysis": preparation_view(latest_task(db, task_type=REVIEW_TASK, opportunity_id=opp_id)),
            "document_ready": document_ready_notice(db, opp_id),
            "budget_status": _budget_panel(opportunity_budget_status(db, opp_id, get_settings())),
            "spend_estimate": _spend_estimate(db, opp_id),
            "deadline_label": deadline_label,
            "deadline_class": deadline_cls,
            "pursuit": pursuit,
            "review_session": review_session,
            "bid_decision": bid_decision,
            "ai_summary": ai_summary,
            "active_tab": active_tab,
            "active_page": "pipeline",
            "workspace_tabs": WORKSPACE_TABS,
            "workspace_more_tabs": WORKSPACE_MORE_TABS,
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


def _budget_panel(status: dict[str, Any]) -> dict[str, Any]:
    limit = int(status.get("limit") or 0)
    used = int(status.get("used") or 0)
    pct = (used / limit) if limit else 0.0
    return {**status, "pct_used": pct, "show_raise": limit > 0 and pct >= 0.8}


def _spend_estimate(db: OrmSession, opp_id: int) -> dict[str, Any]:
    from govcon.ai.spend_guard import estimate_review

    return estimate_review(db, opp_id, get_settings())


def workspace_analyze(request: Request, opp_id: int) -> Response:
    """Queue analysis and compliance without starting a pursuit."""
    from govcon.workflow.preparation import queue_review

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
            from govcon.security.classification import (
                DataClassification,
                opportunity_classification,
            )

            classification = opportunity_classification(db, opp_id, DataClassification.PUBLIC)
            if classification is not DataClassification.PUBLIC:
                raise ValueError(
                    f"External analysis is limited to PUBLIC content. "
                    f"This opportunity is classified as {classification.value}."
                )
            estimate = _spend_estimate(db, opp_id)
            task, created = queue_review(
                db, opportunity_id=opp_id, actor_user_id=actor.id, spend_estimate=estimate,
            )
            record_audit(
                db, action_type="analysis_requested", user_id=actor.id, opportunity_id=opp_id,
                entity_type="tasks", entity_id=task.id,
                new_value={"queued": created, "scope": "analysis_compliance", "estimate": estimate},
            )
    except PermissionDenied as exc:
        return _redirect(target, error=_error_text(exc), request=request)
    except _WORKFLOW_ERRORS as exc:
        return _redirect(target, error=_error_text(exc), request=request)
    notice = (
        "Analysis and compliance queued. Pursuit was not started."
        if created else "Analysis is already queued or running."
    )
    return _redirect(target, notice=notice, request=request)


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
            estimate = _spend_estimate(db, opp_id)
            task, created = queue_preparation(
                db, opportunity_id=opp_id, actor_user_id=actor.id, spend_estimate=estimate,
            )
            record_audit(db, action_type="preparation_requested", user_id=actor.id, opportunity_id=opp_id,
                         entity_type="tasks", entity_id=task.id, new_value={"queued": created})
    except _WORKFLOW_ERRORS as exc:
        return _redirect(target, error=_error_text(exc), request=request)
    return _redirect(target, notice="Preparation queued." if created else "Preparation is already queued or running.", request=request)


def workspace_raise_budget(
    request: Request,
    opp_id: int,
    new_limit: Annotated[str, Form()] = "",
    reason: Annotated[str, Form()] = "",
) -> Response:
    """Audited per-opportunity token cap. Does not change the process default or auto-start tasks."""
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    target = f"/workspace/{opp_id}?tab=overview"
    try:
        raised = int(str(new_limit).replace(",", "").strip())
        if raised <= 0:
            raise ValueError("the new token limit must be a positive integer")
        reason_text = str(reason or "").strip()
        if len(reason_text) < 8:
            raise ValueError("a reason of at least 8 characters is required and is stored in the audit log")
        with session_scope() as db:
            actor = _actor(db, user, "review")
            opp = lock_opportunity(db, opp_id)
            if opp is None:
                raise ValueError("opportunity not found")
            settings = get_settings()
            status = opportunity_budget_status(db, opp_id, settings)
            previous = opp.ai_max_input_tokens
            current_limit = int(status["limit"] or 0)
            used = int(status["used"] or 0)
            spendable = int(status["spendable"] or 0)
            if raised <= current_limit:
                raise ValueError(
                    f"new limit {raised:,} must be greater than the current limit {current_limit:,} "
                    f"({used:,} used of {spendable:,} spendable)"
                )
            opp.ai_max_input_tokens = raised
            record_audit(
                db, action_type="opportunity_budget_raised", user_id=actor.id, opportunity_id=opp_id,
                entity_type="opportunities", entity_id=opp_id,
                old_value={"previous_limit": previous, "used": status["used"], "limit": status["limit"]},
                new_value={
                    "limit": raised,
                    "reason": reason_text,
                    "default_unchanged": settings.ai_max_input_tokens_per_opportunity,
                },
            )
    except PermissionDenied as exc:
        return _redirect(target, error=_error_text(exc), request=request)
    except _WORKFLOW_ERRORS as exc:
        return _redirect(target, error=_error_text(exc), request=request)
    return _redirect(
        target,
        notice=f"Budget for this opportunity raised to {raised:,} input tokens. Resume the parked task on /ops; it will not start on its own.",
        request=request,
    )


def workspace_requirement_override(
    request: Request,
    opp_id: int,
    req_id: int,
    status: Annotated[str, Form()],
    reason: Annotated[str, Form()],
    expected_version: Annotated[str, Form()],
    acknowledge_deterministic_failure: Annotated[str | None, Form()] = None,
) -> Response:
    """Record an authorized human compliance override; the matrix service checks the permission and audits it."""
    from govcon.compliance.matrix import override_requirement

    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    target = f"/workspace/{opp_id}?tab=compliance#req-{req_id}"
    try:
        version = _form_int(expected_version)
        if version is None:
            raise ValueError("reload the page and enter the override again")
        with session_scope() as db:
            actor = _actor(db, user, "override_compliance")
            requirement = db.get(Requirement, req_id)
            if requirement is None or requirement.opportunity_id != opp_id:
                raise ValueError("requirement not found for this opportunity")
            override_requirement(
                db, requirement_id=req_id, status=status, actor=actor, reason=reason,
                expected_version=version, acknowledge_deterministic_failure=acknowledge_deterministic_failure == "yes",
            )
    except _WORKFLOW_ERRORS as exc:
        return _redirect(target, error=_error_text(exc), request=request)
    return _redirect(target, notice=f"Requirement {req_id} override recorded in the audit log.", request=request)
