"""Explicit workspace revision actions using the shared workflow services."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from govcon.collaboration.review_sessions import decision_package_is_stale
from govcon.db import session_scope
from govcon.models import Proposal, Pursuit, ReviewSession, User
from govcon.proposals.versions import (
    ProposalWorkflowError,
    create_proposal_version,
    get_sections_for_version,
)
from govcon.tasks.queue import cancel_active
from govcon.web.routes import (
    _WORKFLOW_ERRORS,
    _actor,
    _error_text,
    _form_int,
    _NeedsLogin,
    _redirect,
    _require_login,
    _templates,
)
from govcon.workflow.invalidation import lock_one, lock_opportunity
from govcon.workflow.transitions import TERMINAL_PURSUIT_STAGES


def checked_proposal(db: Session, opportunity_id: int, expected_version: int | None) -> Proposal:
    if lock_opportunity(db, opportunity_id) is None:
        raise ValueError("opportunity not found")
    review = db.scalar(select(ReviewSession).where(ReviewSession.opportunity_id == opportunity_id))
    if review is None or review.status != "approved_to_bid" or review.final_approval_status != "approved_to_bid":
        raise ProposalWorkflowError("the review must approve this bid before the proposal can change")
    if decision_package_is_stale(db, review):
        raise ProposalWorkflowError("the decision package is stale; re-run preparation and obtain a new bid approval")
    pursuit = db.scalar(select(Pursuit).where(Pursuit.opportunity_id == opportunity_id))
    if pursuit is None or pursuit.stage in TERMINAL_PURSUIT_STAGES:
        raise ProposalWorkflowError("the pursuit is closed; its proposal cannot change")
    proposal = lock_one(db, select(Proposal).where(Proposal.opportunity_id == opportunity_id))
    if proposal is None or proposal.current_version_id is None:
        raise ProposalWorkflowError("no proposal version exists")
    if expected_version is None or proposal.version != expected_version:
        raise ProposalWorkflowError("the proposal changed since this page loaded; reload before trying again")
    return proposal


async def workspace_proposal_revise(request: Request, opp_id: int) -> Response:
    try:
        user = await run_in_threadpool(_require_login, request)
    except _NeedsLogin:
        return _redirect("/login", request=request)
    form = await request.form()
    return await run_in_threadpool(_revise_proposal, request, opp_id, user, form)


def _revise_proposal(request: Request, opp_id: int, user: User, form: Any) -> Response:
    action = str(form.get("action") or "")
    expected = _form_int(form.get("expected_version"))
    target = f"/workspace/{opp_id}?tab=proposal"
    try:
        if action not in {"save", "red_team", "regenerate_without_ai"}:
            raise ValueError("choose a proposal revision action")
        with session_scope() as db:
            actor = _actor(db, user, "approve" if action == "regenerate_without_ai" else "review")
            proposal = checked_proposal(db, opp_id, expected)
            version_id = proposal.current_version_id
            assert version_id is not None
            if proposal.status == "cancelled":
                raise ProposalWorkflowError("the proposal is cancelled")
            if action == "save":
                sections: list[dict[str, Any]] = []
                for section in get_sections_for_version(db, version_id):
                    key = f"section_{section.id}"
                    if key not in form:
                        raise ProposalWorkflowError("the sections changed; reload before saving")
                    sections.append({
                        "section_key": section.section_key, "heading": section.heading,
                        "content": str(form[key]), "requirement_ids": section.requirement_ids,
                        "source_refs": section.source_refs, "sort_order": section.sort_order,
                    })
                if not sections or not any(section["content"].strip() for section in sections):
                    raise ValueError("a proposal must contain some text")
                if sum(len(section["content"].encode("utf-8")) for section in sections) > 2_000_000:
                    raise ValueError("the revised proposal exceeds the text size limit")
                cancel_active(db, opportunity_id=opp_id, task_types=("proposal_generation",),
                              reason="a person saved a proposal revision")
                create_proposal_version(
                    db, proposal_id=proposal.id, created_by=actor.email, actor_id=actor.id,
                    sections=sections, change_summary=str(form.get("change_summary") or "Web revision").strip(),
                )
                message = "Revision saved. Run a red-team review before final approval."
            elif action == "red_team":
                from govcon.compliance.proposal_coverage import check_proposal_coverage
                from govcon.proposals.ai_review import run_proposal_red_team

                run_proposal_red_team(db, opportunity_id=opp_id, proposal_version_id=version_id)
                check_proposal_coverage(db, opp_id, version_id, use_ai=False)
                message = "Red-team review completed. Review its findings before approval."
            else:
                from govcon.proposals.service import generate_proposal

                cancel_active(db, opportunity_id=opp_id, task_types=("proposal_generation",),
                              reason="a person requested a replacement draft without AI")
                generate_proposal(db, opportunity_id=opp_id, actor=actor, skip_ai=True)
                message = "Replacement draft created without AI. Previous versions are preserved."
    except _WORKFLOW_ERRORS as exc:
        status = 409 if "changed" in str(exc) else 400
        return _templates.TemplateResponse(request, "proposal_action_error.html", {
            "user": user, "error": _error_text(exc), "target": target,
            "submitted_text": [str(value) for key, value in form.items() if key.startswith("section_")],
        }, status_code=status)
    return _redirect(target, notice=message, request=request)
