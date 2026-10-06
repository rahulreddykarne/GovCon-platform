"""Proposals endpoints and supporting views."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession
from starlette.concurrency import run_in_threadpool

from govcon.audit import record_audit
from govcon.collaboration.review_sessions import (
    ReviewWorkflowError,
    decision_package_is_stale,
)
from govcon.collaboration.users import AuthError
from govcon.db import session_scope
from govcon.learning.outcomes import record_outcome
from govcon.models import Pursuit, ReviewSession
from govcon.proposals.service import finalize_proposal, record_submission_confirmation
from govcon.tasks.queue import active_task, latest_task, requeue
from govcon.tasks.queue import cancel as cancel_task
from govcon.web.routes.common import (
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
from govcon.workflow.proposal_generation import (
    PROPOSAL_TASK,
    GenerationNotAllowed,
    artifacts_exist,
    queue_proposal_generation,
)
from govcon.workflow.transitions import TERMINAL_PURSUIT_STAGES


def _generation_status(db: OrmSession, opp_id: int) -> dict[str, Any]:
    """The Proposal tab's view of the latest generation task for this opportunity."""
    task = latest_task(db, task_type=PROPOSAL_TASK, opportunity_id=opp_id)
    if task is None:
        return {"task": None, "active": False}
    return {"task": task, "active": task.status in ("queued", "running", "retrying")}


def workspace_proposal_status(request: Request, opp_id: int) -> HTMLResponse:
    """HTMX fragment: generation progress, polled while a task is active."""
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return HTMLResponse("", status_code=401)
    with session_scope() as db:
        try:
            _actor(db, user, "read")
        except AuthError:
            return HTMLResponse("", status_code=403)
        status = _generation_status(db, opp_id)
        return _templates.TemplateResponse(
            request, "workspace/_generation_status.html", {"opp_id": opp_id, **status}
        )


def workspace_proposal_retry(
    request: Request,
    opp_id: int,
    expected_version: Annotated[str | None, Form()] = None,
    without_ai: Annotated[str | None, Form()] = None,
) -> Response:
    """Queue proposal and submission-package generation again for a current bid approval.

    Under the opportunity lock it requires ``approve``, the review version the
    form was rendered with, an ``approved_to_bid`` review, a decision package
    built from the current source, and no existing proposal or submission, so
    a retry only creates missing artifacts and never replaces edited ones. A
    task already queued or running is not duplicated; a task waiting for a
    person is put back in the queue.
    """
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    target = f"/workspace/{opp_id}?tab=proposal"
    version = _form_int(expected_version)
    if version is None:
        return _redirect(target, error="The review changed or the form is out of date; reload and try again.", request=request)
    try:
        with session_scope() as db:
            actor = _actor(db, user, "approve")
            if lock_opportunity(db, opp_id) is None:
                raise ValueError("opportunity not found")
            review = lock_one(db, select(ReviewSession).where(ReviewSession.opportunity_id == opp_id))
            if review is None or review.status != "approved_to_bid" or review.final_approval_status != "approved_to_bid":
                raise ReviewWorkflowError("the bid is not currently approved to bid; there is nothing to generate")
            if review.version != version:
                raise ReviewWorkflowError("the review changed since this page loaded; reload and try again")
            if decision_package_is_stale(db, review):
                raise ReviewWorkflowError(
                    "the AI decision package was built from a superseded source revision; "
                    "regenerate it and re-approve before generating a proposal"
                )
            pursuit = db.scalar(select(Pursuit).where(Pursuit.opportunity_id == opp_id))
            if pursuit is None or pursuit.stage in TERMINAL_PURSUIT_STAGES:
                raise ReviewWorkflowError("the pursuit is closed; there is nothing to generate")
            if artifacts_exist(db, opp_id):
                raise ReviewWorkflowError(
                    "a proposal or submission package already exists; retry only creates missing artifacts"
                )
            active = active_task(db, task_type=PROPOSAL_TASK, opportunity_id=opp_id)
            if active is not None and active.status not in ("waiting_for_input", "waiting_for_budget"):
                raise ReviewWorkflowError("proposal generation is already queued or running")
            for_placeholder = (without_ai or "").strip() == "1"
            if active is not None and bool((active.payload or {}).get("without_ai")) == for_placeholder:
                task = requeue(db, active, actor_user_id=actor.id, reason="retried from the Proposal tab")
                outcome = "requeued"
            else:
                if active is not None:
                    cancel_task(db, active, reason="replaced by a retry with different options",
                                actor_user_id=actor.id)
                queued = queue_proposal_generation(
                    db, opportunity_id=opp_id, actor_user_id=actor.id, without_ai=for_placeholder
                )
                if queued is None:  # pragma: no cover - artifacts were checked under the lock
                    raise ReviewWorkflowError("a proposal or submission package already exists")
                task, _ = queued
                outcome = "queued"
            record_audit(
                db,
                action_type="proposal_generation_retried",
                user_id=actor.id,
                opportunity_id=opp_id,
                entity_type="review_sessions",
                entity_id=review.id,
                new_value={"outcome": outcome, "task_id": task.id, "without_ai": for_placeholder,
                           "review_version": review.version},
            )
    except (_WORKFLOW_ERRORS + (GenerationNotAllowed,)) as exc:
        return _redirect(target, error=_error_text(exc), request=request)
    return _redirect(target, notice="Proposal generation queued.", request=request)


def workspace_proposal_approve(
    request: Request,
    opp_id: int,
    decision: Annotated[str, Form()],
    expected_version: Annotated[str | None, Form()] = None,
    override_reason: Annotated[str | None, Form()] = None,
) -> Response:
    """Final proposal decision through ``finalize_proposal`` and the compliance gate."""
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    target = f"/workspace/{opp_id}?tab=proposal"
    version = _form_int(expected_version)
    if version is None:
        return _redirect(target, error="The proposal changed or the form is out of date; reload and try again.", request=request)
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
        return _redirect(target, error=_error_text(exc), request=request)
    return _redirect(target, notice=f"Proposal decision recorded: {decision}.", request=request)


def workspace_submission_approve(
    request: Request,
    opp_id: int,
    expected_version: Annotated[str | None, Form()] = None,
    confirmation_number: Annotated[str | None, Form()] = None,
    confirmation_notes: Annotated[str | None, Form()] = None,
    submitted_at: Annotated[str | None, Form()] = None,
) -> Response:
    """Record the human's manual submission with confirmation evidence."""
    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    target = f"/workspace/{opp_id}?tab=submission"
    version = _form_int(expected_version)
    if version is None:
        return _redirect(target, error="The submission changed or the form is out of date; reload and try again.", request=request)
    when: datetime | None = None
    if submitted_at and submitted_at.strip():
        try:
            when = datetime.fromisoformat(submitted_at.strip())
        except ValueError:
            return _redirect(target, error="Submission time is not a valid date/time.", request=request)
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
        return _redirect(target, error=_error_text(exc), request=request)
    return _redirect(target, notice="Submission recorded.", request=request)


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
) -> Response:
    # Async only to read the raw form; database work runs in the thread pool.
    try:
        user = await run_in_threadpool(_require_login, request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    target = f"/workspace/{opp_id}?tab=submission"

    valid_outcomes = ("won", "lost", "no_bid", "cancelled")
    if outcome not in valid_outcomes:
        return _redirect(target, error=f"Unknown outcome {outcome!r}.", request=request)

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

        def _record() -> None:
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

        await run_in_threadpool(_record)
    except _WORKFLOW_ERRORS as exc:
        return _redirect(target, error=_error_text(exc), request=request)
    return _redirect(target, notice=f"Outcome recorded: {outcome}.", request=request)


def workspace_outcome_suggestion(request: Request, opp_id: int, suggestion_id: int, action: str) -> Response:
    """Confirm a suggested outcome through ``record_outcome``, or dismiss it (ADR-073)."""
    from govcon.learning.outcomes import record_outcome
    from govcon.models import OutcomeSuggestion

    try:
        user = _require_login(request)
    except _NeedsLogin:
        return RedirectResponse("/login", status_code=303)
    target = f"/workspace/{opp_id}?tab=submission"
    if action not in ("confirm", "dismiss"):
        return HTMLResponse("Unknown action", status_code=404)
    try:
        with session_scope() as db:
            actor = _actor(db, user, "approve")
            suggestion = lock_one(db, select(OutcomeSuggestion).where(
                OutcomeSuggestion.id == suggestion_id, OutcomeSuggestion.opportunity_id == opp_id))
            if suggestion is None:
                raise ValueError("suggestion not found")
            if suggestion.status != "suggested":
                raise ValueError(f"this suggestion was already {suggestion.status}")
            if action == "confirm":
                if suggestion.suggested_outcome is None:
                    raise ValueError("this record does not establish an outcome; record it with the form instead")
                record_outcome(
                    db, opportunity_id=opp_id, outcome=suggestion.suggested_outcome, actor=actor,
                    awarded_vendor_uei=suggestion.awardee_uei, awarded_vendor_name=suggestion.awardee_name,
                    award_amount=float(suggestion.award_amount) if suggestion.award_amount is not None else None,
                    award_date=suggestion.award_date,
                    government_feedback=f"Confirmed from {suggestion.source} record {suggestion.source_ref}",
                )
            suggestion.status = "confirmed" if action == "confirm" else "dismissed"
            suggestion.decided_by_user_id = actor.id
            suggestion.decided_at = datetime.now(UTC)
            record_audit(db, action_type=f"outcome_suggestion_{suggestion.status}", user_id=actor.id,
                         opportunity_id=opp_id, entity_type="outcome_suggestions", entity_id=suggestion.id,
                         new_value={"source": suggestion.source, "source_ref": suggestion.source_ref,
                                    "suggested_outcome": suggestion.suggested_outcome})
    except _WORKFLOW_ERRORS as exc:
        return _redirect(target, error=_error_text(exc), request=request)
    return _redirect(target, notice="Outcome recorded." if action == "confirm" else "Suggestion dismissed.", request=request)
