"""Immutable proposal version management (Phase 11).

Every save of a proposal creates a new ``ProposalVersion`` row. Existing
versions are never overwritten. Callers access the latest version via
``Proposal.current_version_id``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.models import Proposal, ProposalSection, ProposalVersion


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
) -> ProposalVersion:
    """Create a new immutable version for a proposal.

    ``sections`` is a list of dicts with keys:
      section_key, heading, content, requirement_ids, source_refs, sort_order

    The new version becomes the ``current_version_id`` on the parent
    ``Proposal`` record.
    """
    proposal = session.get(Proposal, proposal_id)
    if proposal is None:
        raise ValueError(f"Proposal {proposal_id} not found")

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

    pv = ProposalVersion(
        proposal_id=proposal_id,
        version_number=next_number,
        created_by=created_by,
        provider=provider,
        model=model,
        change_summary=change_summary,
        full_text=full_text,
        version_metadata=metadata,
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

    # Update proposal's current_version_id
    proposal.current_version_id = pv.id
    session.flush()

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


def get_sections_for_version(session: Session, version_id: int) -> list[ProposalSection]:
    """Return sections for a proposal version in sort_order."""
    return list(
        session.scalars(
            select(ProposalSection)
            .where(ProposalSection.proposal_version_id == version_id)
            .order_by(ProposalSection.sort_order)
        ).all()
    )
