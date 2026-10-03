"""Allowed state transitions for review sessions, pursuits, proposals, and submissions.

Every service that changes one of these states checks the table first. A
transition that is not listed is rejected, so a route, CLI command, or MCP
tool cannot move a record into a state its gate never approved.

``GATED_PURSUIT_STAGES`` can only be entered through the named service:

- ``bid_approved`` / ``no_bid`` — ``finalize_approval``
- ``ready_to_submit`` — ``move_to_ready_to_submit`` (compliance gate)
- ``submitted`` — ``record_submission_confirmation``
- ``won`` / ``lost`` — ``record_outcome`` after a recorded submission
"""

from __future__ import annotations


class InvalidTransition(ValueError):
    """A requested state change is not allowed from the current state."""

    def __init__(self, kind: str, current: str | None, target: str) -> None:
        self.kind = kind
        self.current = current
        self.target = target
        super().__init__(f"{kind} cannot move from {current!r} to {target!r}")


REVIEW_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"ready_for_review", "under_review", "review_complete", "approval_pending", "approved_to_bid", "no_bid"}),
    "ready_for_review": frozenset({"under_review", "review_complete", "approval_pending", "approved_to_bid", "no_bid", "returned_for_review"}),
    "under_review": frozenset({"ready_for_review", "review_complete", "approval_pending", "approved_to_bid", "no_bid", "returned_for_review"}),
    "review_complete": frozenset({"approval_pending", "under_review", "approved_to_bid", "no_bid", "returned_for_review"}),
    "approval_pending": frozenset({"under_review", "review_complete", "approved_to_bid", "no_bid", "returned_for_review"}),
    "returned_for_review": frozenset({"ready_for_review", "under_review", "review_complete", "approval_pending", "approved_to_bid", "no_bid"}),
    # A decided session only reopens (material source change, or the approver returns it).
    "approved_to_bid": frozenset({"under_review", "ready_for_review", "returned_for_review"}),
    "no_bid": frozenset({"under_review", "ready_for_review", "returned_for_review"}),
}

# ``review`` is the post-drafting submission-validation stage (spec order:
# drafting → review → ready_to_submit). A revoked bid approval returns the
# pursuit to ``evaluating``.
PURSUIT_TRANSITIONS: dict[str, frozenset[str]] = {
    "evaluating": frozenset({"sourcing", "bid_approved", "no_bid", "cancelled"}),
    "sourcing": frozenset({"evaluating", "drafting", "review", "bid_approved", "ready_to_submit", "no_bid", "cancelled"}),
    "bid_approved": frozenset({"evaluating", "sourcing", "drafting", "review", "ready_to_submit", "no_bid", "cancelled"}),
    "drafting": frozenset({"evaluating", "sourcing", "review", "bid_approved", "ready_to_submit", "no_bid", "cancelled"}),
    "review": frozenset({"evaluating", "sourcing", "drafting", "bid_approved", "ready_to_submit", "no_bid", "cancelled"}),
    "ready_to_submit": frozenset({"evaluating", "drafting", "review", "bid_approved", "submitted", "cancelled"}),
    "submitted": frozenset({"won", "lost", "cancelled"}),
    "won": frozenset(),
    "lost": frozenset(),
    "cancelled": frozenset(),
    # A no-bid decision can be reopened when the source materially changes.
    "no_bid": frozenset({"evaluating"}),
}

GATED_PURSUIT_STAGES = frozenset({"bid_approved", "no_bid", "ready_to_submit", "submitted", "won", "lost"})
TERMINAL_PURSUIT_STAGES = frozenset({"won", "lost", "cancelled", "no_bid"})

PROPOSAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"ai_generated", "red_teamed", "returned_for_fix", "cancelled"}),
    "ai_generated": frozenset({"draft", "red_teamed", "final_approved", "returned_for_fix", "cancelled"}),
    "red_teamed": frozenset({"draft", "ai_generated", "final_approved", "returned_for_fix", "cancelled"}),
    "final_approved": frozenset({"draft", "ai_generated", "returned_for_fix", "cancelled"}),
    "returned_for_fix": frozenset({"draft", "ai_generated", "red_teamed", "cancelled"}),
    "cancelled": frozenset(),
}

APPROVABLE_PROPOSAL_STATUSES = frozenset({"ai_generated", "red_teamed"})

SUBMISSION_TRANSITIONS: dict[str, frozenset[str]] = {
    "preparing": frozenset({"ready", "withdrawn"}),
    "ready": frozenset({"preparing", "submitted", "withdrawn"}),
    "submitted": frozenset({"confirmed", "failed", "withdrawn"}),
    "confirmed": frozenset(),
    "failed": frozenset({"preparing", "ready", "withdrawn"}),
    "withdrawn": frozenset(),
}

_TABLES = {
    "review_session": REVIEW_TRANSITIONS,
    "pursuit": PURSUIT_TRANSITIONS,
    "proposal": PROPOSAL_TRANSITIONS,
    "submission": SUBMISSION_TRANSITIONS,
}


def can_transition(kind: str, current: str | None, target: str) -> bool:
    """Return whether ``kind`` may move from ``current`` to ``target``.

    Staying in the same state is always allowed (idempotent writes).
    """
    table = _TABLES[kind]
    if target not in table:
        return False
    if current == target:
        return True
    if current is None:
        return True
    return target in table.get(current, frozenset())


def require_transition(kind: str, current: str | None, target: str) -> None:
    if not can_transition(kind, current, target):
        raise InvalidTransition(kind, current, target)
