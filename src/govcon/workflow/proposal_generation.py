"""Queue proposal and submission-package generation for an approved bid (ADR-062).

Bid approval queues the work in the approval's own transaction; a worker
drafts with no transaction open and publishes under the opportunity lock. The
same preconditions guard queueing, the worker's prepare step and its publish,
so a withdrawn approval, a stale decision package or an existing proposal
always stops generation and never overwrites human work.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.models import Proposal, Pursuit, ReviewSession, Submission, Task
from govcon.tasks import queue
from govcon.workflow.source_revision import current_source_revision
from govcon.workflow.transitions import TERMINAL_PURSUIT_STAGES

PROPOSAL_TASK = "proposal_generation"


class GenerationNotAllowed(Exception):
    """Why generation cannot run now. ``kind`` selects the task outcome."""

    def __init__(self, message: str, *, kind: str) -> None:
        super().__init__(message)
        self.kind = kind  # "withdrawn" | "stale_package" | "closed" | "exists"


def commercial_hash(pursuit: Pursuit | None) -> str:
    facts: dict[str, Any] = {}
    if pursuit is not None:
        for name in ("supplier", "sourcing_cost", "quote_price"):
            value = getattr(pursuit, name)
            facts[name] = str(value) if isinstance(value, Decimal) else value
    return hashlib.sha256(json.dumps(facts, sort_keys=True, default=str).encode()).hexdigest()


def artifacts_exist(session: Session, opportunity_id: int) -> bool:
    return (
        session.scalar(select(Proposal.id).where(Proposal.opportunity_id == opportunity_id).limit(1)) is not None
        or session.scalar(select(Submission.id).where(Submission.opportunity_id == opportunity_id).limit(1))
        is not None
    )


def check_generation_allowed(session: Session, opportunity_id: int) -> ReviewSession:
    """Raise ``GenerationNotAllowed`` unless a proposal may be generated now."""
    from govcon.collaboration.review_sessions import decision_package_is_stale

    review = session.scalar(select(ReviewSession).where(ReviewSession.opportunity_id == opportunity_id))
    if review is None or review.status != "approved_to_bid" or review.final_approval_status != "approved_to_bid":
        raise GenerationNotAllowed("the bid is no longer approved to bid", kind="withdrawn")
    pursuit = session.scalar(select(Pursuit).where(Pursuit.opportunity_id == opportunity_id))
    if pursuit is None or pursuit.stage in TERMINAL_PURSUIT_STAGES:
        raise GenerationNotAllowed("the pursuit is closed", kind="closed")
    if artifacts_exist(session, opportunity_id):
        raise GenerationNotAllowed(
            "a proposal or submission package already exists; generation only creates missing artifacts",
            kind="exists",
        )
    if decision_package_is_stale(session, review):
        raise GenerationNotAllowed(
            "the AI decision package was built from a superseded source revision", kind="stale_package"
        )
    return review


def generation_inputs(session: Session, opportunity_id: int, review: ReviewSession, *,
                      without_ai: bool = False) -> dict[str, Any]:
    """The inputs a generation result depends on; a change makes the result stale."""
    pursuit = session.scalar(select(Pursuit).where(Pursuit.opportunity_id == opportunity_id))
    return {
        "source_revision": str(current_source_revision(session, opportunity_id)),
        "review_id": review.id,
        "approved_at": review.approved_at.isoformat() if review.approved_at else None,
        "decision_package_id": review.ai_decision_package_id,
        "commercial_hash": commercial_hash(pursuit),
        "without_ai": without_ai,
    }


def queue_proposal_generation(
    session: Session,
    *,
    opportunity_id: int,
    actor_user_id: int | None,
    without_ai: bool = False,
) -> tuple[Task, bool] | None:
    """Queue generation in the caller's transaction; None when nothing is missing.

    Raises ``GenerationNotAllowed`` for a withdrawn approval, closed pursuit or
    stale package; returns None (queues nothing) when artifacts already exist,
    so a re-approval never replaces a proposal people may have edited.
    """
    try:
        review = check_generation_allowed(session, opportunity_id)
    except GenerationNotAllowed as exc:
        if exc.kind == "exists":
            return None
        raise
    return queue.enqueue(
        session,
        task_type=PROPOSAL_TASK,
        opportunity_id=opportunity_id,
        input_revision=generation_inputs(session, opportunity_id, review, without_ai=without_ai),
        payload={"without_ai": without_ai},
        actor_user_id=actor_user_id,
    )
