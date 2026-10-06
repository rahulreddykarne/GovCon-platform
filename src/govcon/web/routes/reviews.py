"""Reviews endpoints and supporting views."""

from __future__ import annotations

from typing import Annotated

from fastapi import Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from govcon.audit import record_audit
from govcon.collaboration.assignments import assign_reviewer
from govcon.collaboration.comments import add_comment
from govcon.collaboration.review_sessions import (
    complete_assignment,
    ensure_review_session,
    finalize_approval,
    recalculate_quorum,
)
from govcon.collaboration.users import require_permission
from govcon.db import session_scope
from govcon.models import Opportunity, User
from govcon.web.routes.common import (
    _WORKFLOW_ERRORS,
    _actor,
    _error_text,
    _form_int,
    _NeedsLogin,
    _redirect,
    _require_login,
)
from govcon.workflow.invalidation import lock_opportunity


def workspace_comment(
    request: Request,
    opp_id: int,
    body: Annotated[str, Form()] = "",
    user_recommendation: Annotated[str | None, Form()] = None,
    topic: Annotated[str | None, Form()] = None,
) -> Response:
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
                defer_ai_validation=True,
            )
    except _WORKFLOW_ERRORS as exc:
        return _redirect(target, error=_error_text(exc), request=request)
    return _redirect(target, notice="Comment added.", request=request)


_ANALYSIS_TABS = {"market": "market", "supplier": "products", "pricing": "pricing"}


def workspace_run_analysis(request: Request, opp_id: int, kind: str) -> Response:
    """Queue the market / supplier / pricing AI analysis from its workspace tab (ADR-064).

    Inputs and data-classification policy are checked in the request, so a
    missing supplier or a blocked call is reported at once; a worker then
    makes the AI call outside any transaction.
    """
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    from govcon.ai.structured import StructuredCallError
    from govcon.intelligence.analysis_tasks import queue_analysis

    tab = _ANALYSIS_TABS.get(kind)
    if tab is None:
        return HTMLResponse("Unknown analysis", status_code=404)
    target = f"/workspace/{opp_id}?tab={tab}"
    try:
        with session_scope() as db:
            actor = _actor(db, user, "review")
            if lock_opportunity(db, opp_id) is None:
                raise ValueError("opportunity not found")
            task, created = queue_analysis(db, opportunity_id=opp_id, kind=kind, actor_user_id=actor.id)
            record_audit(
                db,
                action_type="ai_analysis_requested",
                user_id=actor.id,
                opportunity_id=opp_id,
                entity_type="tasks",
                entity_id=task.id,
                new_value={"kind": kind, "task_id": task.id, "queued": created},
            )
    except StructuredCallError as exc:
        return _redirect(target, error=f"Analysis not run ({exc.reason}): {exc.detail}", request=request)
    except _WORKFLOW_ERRORS as exc:
        return _redirect(target, error=_error_text(exc), request=request)
    if not created:
        return _redirect(target, notice=f"{kind.title()} analysis is already queued for these inputs.", request=request)
    return _redirect(target, notice=f"{kind.title()} analysis queued; this tab shows the result when it finishes.", request=request)


def workspace_assign_reviewer(
    request: Request,
    opp_id: int,
    user_id: Annotated[int, Form()],
    assignment_role: Annotated[str, Form()] = "reviewer",
) -> Response:
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
        return _redirect(target, error=_error_text(exc), request=request)
    return _redirect(target, notice="Reviewer assigned.", request=request)


def workspace_complete_review(
    request: Request,
    opp_id: int,
    recommendation: Annotated[str | None, Form()] = None,
    action: Annotated[str, Form()] = "approve_continue",
    agree_with_ai_assessment: Annotated[str | None, Form()] = None,
    second_review_reason: Annotated[str | None, Form()] = None,
) -> Response:
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
        return _redirect(target, error=_error_text(exc), request=request)
    return _redirect(target, notice="Review recorded.", request=request)


def workspace_approve(
    request: Request,
    opp_id: int,
    decision: Annotated[str, Form()],
    expected_version: Annotated[str | None, Form()] = None,
    override_reason: Annotated[str | None, Form()] = None,
    no_bid_reason: Annotated[str | None, Form()] = None,
    no_bid_category: Annotated[str | None, Form()] = None,
) -> Response:
    """Final bid decision through ``finalize_approval`` (quorum, override, version, audit).

    Approving to bid also queues proposal generation in the same transaction
    (ADR-062); the response does not wait for it.
    """
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    target = f"/workspace/{opp_id}?tab=review"
    version = _form_int(expected_version)
    if version is None:
        return _redirect(target, error="The review changed or the form is out of date; reload and try again.", request=request)
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
                no_bid_reason=(no_bid_reason or "").strip() or None,
                no_bid_category=(no_bid_category or "").strip() or None,
            )
    except _WORKFLOW_ERRORS as exc:
        return _redirect(target, error=_error_text(exc), request=request)
    notice = f"Decision recorded: {decision.replace('_', ' ')}."
    if decision == "approve_to_bid":
        notice += " Proposal generation is queued; follow it on the Proposal tab."
    return _redirect(target, notice=notice, request=request)
