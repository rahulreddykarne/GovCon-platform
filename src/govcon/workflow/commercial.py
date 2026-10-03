"""Commercial edits before submission and append-only corrections afterwards."""

from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.audit import record_audit
from govcon.collaboration.users import require_permission
from govcon.concurrency import apply_versioned_update
from govcon.models import (
    Opportunity,
    Proposal,
    Pursuit,
    ReviewSession,
    Submission,
    User,
)
from govcon.workflow.invalidation import apply_source_change, lock_one, lock_opportunity

COMMERCIAL_FIELDS = frozenset({"quote_price", "sourcing_cost", "supplier", "notes"})
LOCKED_STAGES = frozenset({"submitted", "won", "lost", "cancelled", "no_bid"})


def validated_commercial_changes(changes: dict[str, Any], *, allow_clear: bool = False) -> dict[str, Any]:
    """Checked values; with ``allow_clear`` a ``None`` value clears that field."""
    if not changes or set(changes) - COMMERCIAL_FIELDS:
        raise ValueError("provide only quote_price, sourcing_cost, supplier, or notes")
    result = dict(changes)
    for field in ("quote_price", "sourcing_cost"):
        if field in result and not (allow_clear and result[field] is None):
            try:
                value = Decimal(str(result[field]))
            except (InvalidOperation, ValueError) as exc:
                raise ValueError(f"{field} must be a finite nonnegative number") from exc
            if not value.is_finite() or value < 0:
                raise ValueError(f"{field} must be finite and nonnegative")
            result[field] = value
    for field in ("supplier", "notes"):
        if field in result and not (allow_clear and result[field] is None) and not isinstance(result[field], str):
            raise ValueError(f"{field} must be text")
    return result


def update_commercial_facts(
    session: Session, opportunity_id: int, *, actor: User, expected_version: int,
    changes: dict[str, Any], via: str,
) -> tuple[Pursuit, bool]:
    """Edit a pursuit's pre-submission commercial facts; returns ``(pursuit, changed)``.

    Same rules as MCP ``update_pursuit``: the opportunity lock first, the
    version the editor saw, no edits after submission (an approver records an
    append-only correction instead), an audit row, and dependent review,
    proposal and submission decisions revoked. ``None`` clears a field.
    """
    require_permission(actor, "review")
    changes = validated_commercial_changes(changes, allow_clear=True)
    if lock_opportunity(session, opportunity_id) is None:
        raise ValueError("opportunity not found")
    pursuit = lock_one(session, select(Pursuit).where(Pursuit.opportunity_id == opportunity_id))
    if pursuit is None:
        raise ValueError("start a pursuit for this opportunity before recording its commercial facts")
    if pursuit.version != expected_version:
        raise ValueError("the pursuit changed since this page was loaded; reload it and enter the values again")
    if pursuit.stage in LOCKED_STAGES:
        raise ValueError("commercial facts are locked once the bid is submitted or closed; an approver must "
                         "record an append-only correction instead")
    changes = {key: value for key, value in changes.items() if getattr(pursuit, key) != value}
    if not changes:
        return pursuit, False
    previous = apply_versioned_update(session, pursuit, expected_version, changes)
    record_audit(
        session, action_type="pursuit_updated", user_id=actor.id, opportunity_id=opportunity_id,
        entity_type="pursuits", entity_id=pursuit.id,
        old_value={k: (str(v) if isinstance(v, Decimal) else v) for k, v in previous.items()},
        new_value={**{k: (str(v) if isinstance(v, Decimal) else v) for k, v in changes.items()}, "via": via},
    )
    invalidate_commercial_decisions(session, opportunity_id, actor=actor)
    return pursuit, True


def invalidate_commercial_decisions(session: Session, opportunity_id: int, *, actor: User) -> None:
    if not any(session.scalar(select(model.id).where(model.opportunity_id == opportunity_id).limit(1))
               for model in (ReviewSession, Proposal, Submission)):
        return
    reason = "commercial facts changed; review and regenerate the proposal"
    apply_source_change(session, opportunity_id, level="material", reason=reason, actor_id=actor.id)
    proposal = lock_one(session, select(Proposal).where(Proposal.opportunity_id == opportunity_id))
    if proposal is not None and proposal.status in {"draft", "ai_generated", "red_teamed"}:
        old = {"status": proposal.status, "version": proposal.version}
        apply_versioned_update(session, proposal, proposal.version, {"status": "returned_for_fix"})
        record_audit(session, user_id=actor.id, opportunity_id=opportunity_id,
                     action_type="proposal_commercial_facts_invalidated", entity_type="proposals",
                     entity_id=proposal.id, old_value=old, new_value={"status": proposal.status, "reason": reason})


def record_commercial_correction(
    session: Session, opportunity_id: int, *, actor: User, expected_version: int,
    changes: dict[str, Any], reason: str,
) -> dict[str, Any]:
    """Record an approver's correction without rewriting any submitted facts.

    Corrections are audit records anchored to the submission and its package.
    They do not replace the pursuit, submitted files, outcome, or approvals.
    """
    require_permission(actor, "approve")
    if not reason or len(reason.strip()) < 10:
        raise ValueError("a correction reason of at least 10 characters is required")
    changes = validated_commercial_changes(changes)
    lock_one(session, select(Opportunity).where(Opportunity.id == opportunity_id))
    pursuit = lock_one(session, select(Pursuit).where(Pursuit.opportunity_id == opportunity_id))
    if pursuit is None or pursuit.stage not in {"submitted", "won", "lost", "cancelled"}:
        raise ValueError("corrections require a submitted pursuit")
    if pursuit.version != expected_version:
        raise ValueError("stale pursuit version; reload before recording a correction")
    submission = lock_one(session, select(Submission).where(
        Submission.opportunity_id == opportunity_id, Submission.submitted_at.is_not(None)
    ).order_by(Submission.id.desc()).limit(1))
    if submission is None:
        raise ValueError("corrections require an actual submission record")
    original = {field: str(getattr(pursuit, field)) if isinstance(getattr(pursuit, field), Decimal)
                else getattr(pursuit, field) for field in changes}
    corrected = {field: str(value) if isinstance(value, Decimal) else value for field, value in changes.items()}
    event = record_audit(
        session, user_id=actor.id, opportunity_id=opportunity_id,
        action_type="submitted_commercial_correction", entity_type="pursuits", entity_id=pursuit.id,
        old_value={"submitted_facts": original, "pursuit_version": pursuit.version},
        new_value={"correction": corrected, "reason": reason.strip(), "submission_id": submission.id,
                   "package_manifest_hash": submission.package_manifest_hash},
    )
    return {"audit_event_id": event.id, "submission_id": submission.id, "correction": corrected}
