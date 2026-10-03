"""Immutable proposal version management (Phase 11).

Every save of a proposal creates a new ``ProposalVersion`` row. Existing
versions are never overwritten. Callers access the latest version via
``Proposal.current_version_id``.

A new version changes what would be submitted, so it resets the proposal to
``draft`` (or ``ai_generated`` for a regenerated AI draft). When the proposal
was final-approved, the approval and submission readiness are invalidated.
No version can be added to a cancelled proposal or after submission.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.audit import record_audit
from govcon.models import Proposal, ProposalSection, ProposalVersion, Pursuit
from govcon.workflow.invalidation import (
    invalidate_proposal_approval,
    invalidate_submission_readiness,
    lock_one,
    lock_opportunity,
)
from govcon.workflow.source_revision import SOURCE_REVISION_KEY, current_source_revision
from govcon.workflow.transitions import InvalidTransition, require_transition

_CLOSED_PURSUIT_STAGES = frozenset({"submitted", "won", "lost", "cancelled", "no_bid"})


class ProposalWorkflowError(ValueError):
    """A proposal or submission action is not allowed in the current state."""


def create_proposal_version(
    session: Session,
    *,
    proposal_id: int,
    created_by: str,
    sections: list[dict[str, Any]],
    provider: str | None = None,
    model: str | None = None,
    change_summary: str | None = None,
    full_text: str | None = None,
    metadata: dict[str, Any] | None = None,
    status_after: str = "draft",
    actor_id: int | None = None,
) -> ProposalVersion:
    """Create a new immutable version for a proposal.

    ``sections`` is a list of dicts with keys:
      section_key, heading, content, requirement_ids, source_refs, sort_order

    The new version becomes the ``current_version_id`` on the parent
    ``Proposal`` record, and the proposal moves to ``status_after``. The
    version metadata records the source revision it was written against.
    """
    if status_after not in {"draft", "ai_generated"}:
        raise ValueError("a new proposal version can only leave the proposal draft or ai_generated")
    opportunity_id = session.scalar(select(Proposal.opportunity_id).where(Proposal.id == proposal_id))
    if opportunity_id is not None:
        lock_opportunity(session, opportunity_id)
    proposal = lock_one(session, select(Proposal).where(Proposal.id == proposal_id))
    if proposal is None:
        raise ValueError(f"Proposal {proposal_id} not found")
    if proposal.status == "cancelled":
        raise ProposalWorkflowError("the bid was cancelled; no new proposal versions can be created")
    pursuit = session.get(Pursuit, proposal.pursuit_id)
    if pursuit is not None and pursuit.stage in _CLOSED_PURSUIT_STAGES:
        raise ProposalWorkflowError(
            f"pursuit is {pursuit.stage!r}; the proposal can no longer change"
        )
    was_approved = proposal.status == "final_approved"
    if not was_approved:
        try:
            require_transition("proposal", proposal.status, status_after)
        except InvalidTransition as exc:
            raise ProposalWorkflowError(str(exc)) from exc

    # Determine next version number
    latest = _latest_version(session, proposal_id)
    next_number = (latest.version_number + 1) if latest is not None else 1

    # Build full_text from sections if not provided
    if full_text is None:
        parts: list[str] = []
        for s in sections:
            heading = s.get("heading") or s.get("section_key") or ""
            content = s.get("content") or ""
            parts.append(f"## {heading}\n\n{content}")
        full_text = "\n\n".join(parts)

    version_metadata = dict(metadata or {})
    version_metadata[SOURCE_REVISION_KEY] = current_source_revision(session, proposal.opportunity_id)
    pv = ProposalVersion(
        proposal_id=proposal_id,
        version_number=next_number,
        created_by=created_by,
        provider=provider,
        model=model,
        change_summary=change_summary,
        full_text=full_text,
        version_metadata=version_metadata,
    )
    session.add(pv)
    session.flush()  # get pv.id

    # Create sections
    for i, sec in enumerate(sections):
        ps = ProposalSection(
            proposal_version_id=pv.id,
            section_key=sec.get("section_key"),
            heading=sec.get("heading"),
            sort_order=sec.get("sort_order", i),
            content=sec.get("content") or "",
            requirement_ids=sec.get("requirement_ids") or [],
            source_refs=sec.get("source_refs"),
            status="draft",
        )
        session.add(ps)

    session.flush()

    if was_approved:
        # The approved text is no longer the text that would be submitted.
        reason = f"proposal version {next_number} created after final approval"
        invalidate_proposal_approval(
            session, proposal.opportunity_id, reason=reason, actor_id=actor_id, target_status=status_after
        )
        invalidate_submission_readiness(session, proposal.opportunity_id, reason=reason, actor_id=actor_id)
    old_status = proposal.status
    proposal.status = status_after
    proposal.current_version_id = pv.id
    proposal.version = (proposal.version or 1) + 1
    session.flush()
    record_audit(
        session,
        action_type="proposal_version_created",
        user_id=actor_id,
        opportunity_id=proposal.opportunity_id,
        entity_type="proposal_versions",
        entity_id=pv.id,
        old_value={"status": old_status},
        new_value={"version_number": next_number, "status": proposal.status, "created_by": created_by},
    )
    return pv


def _latest_version(session: Session, proposal_id: int) -> ProposalVersion | None:
    return session.scalars(
        select(ProposalVersion)
        .where(ProposalVersion.proposal_id == proposal_id)
        .order_by(ProposalVersion.version_number.desc())
        .limit(1)
    ).first()


def latest_proposal_version(session: Session, proposal_id: int) -> ProposalVersion | None:
    """Return the highest-numbered version for a proposal."""
    return _latest_version(session, proposal_id)


def list_proposal_versions(session: Session, proposal_id: int) -> list[ProposalVersion]:
    """Return all versions in ascending order."""
    return list(
        session.scalars(
            select(ProposalVersion)
            .where(ProposalVersion.proposal_id == proposal_id)
            .order_by(ProposalVersion.version_number)
        ).all()
    )


def version_for_opportunity(session: Session, opportunity_id: int, proposal_version_id: int) -> tuple[Proposal, ProposalVersion]:
    """The version and its proposal, only if both belong to ``opportunity_id``.

    Raises ``ValueError`` before any caller writes evidence, findings or
    package records for one opportunity from another's proposal.
    """
    version = session.get(ProposalVersion, proposal_version_id)
    if version is None:
        raise ValueError(f"proposal version not found: {proposal_version_id}")
    proposal = session.get(Proposal, version.proposal_id)
    if proposal is None or proposal.opportunity_id != opportunity_id:
        raise ValueError(f"proposal version {proposal_version_id} does not belong to opportunity {opportunity_id}")
    return proposal, version


def get_sections_for_version(session: Session, version_id: int) -> list[ProposalSection]:
    """Return sections for a proposal version in sort_order."""
    return list(
        session.scalars(
            select(ProposalSection)
            .where(ProposalSection.proposal_version_id == version_id)
            .order_by(ProposalSection.sort_order)
        ).all()
    )
