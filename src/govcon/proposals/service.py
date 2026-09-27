"""Proposal orchestration service (Phase 11).

Main entry point for proposal lifecycle management. The trigger is
``review_session.final_approval_status = approved_to_bid``.

Sequence (§17, Appendix B):
  1. Source extraction complete  (Phase 9)
  2. Compliance matrix reviewed  (Phase 9)
  3. Bid approved                ← trigger here
  4. [Optional] Pricing/supplier evidence entered
  5. Proposal v1 generated       ← generate_proposal()
  6. Coverage validation         ← coverage check via compliance.proposal_coverage
  7. Red-team reviewer           ← run_proposal_red_team()
  8. User edits → new immutable version
  9. Final consistency review
  10. Submission checklist generated
  11. Human marks ready          ← finalize_proposal()
  12. Human submits              (manual)
  13. Confirmation recorded      ← record_submission_confirmation()
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.audit import record_audit
from govcon.collaboration.users import require_permission
from govcon.compliance.matrix import active_requirements
from govcon.compliance.metrics import coverage_summary
from govcon.compliance.proposal_coverage import check_proposal_coverage
from govcon.compliance.submission_preflight import (
    ReadinessBlocked,
    move_to_ready_to_submit,
)
from govcon.concurrency import apply_versioned_update
from govcon.config import Settings, get_settings
from govcon.models import (
    Notification,
    Opportunity,
    Proposal,
    ProposalVersion,
    Pursuit,
    ReviewSession,
    Submission,
    User,
)
from govcon.proposals.ai_review import run_proposal_red_team
from govcon.proposals.drafting import draft_proposal
from govcon.proposals.versions import (
    create_proposal_version,
    get_sections_for_version,
    latest_proposal_version,
)
from govcon.submissions.service import generate_submission_package

logger = logging.getLogger("govcon.proposals.service")


def get_or_create_proposal(
    session: Session,
    *,
    opportunity_id: int,
    pursuit_id: int,
) -> Proposal:
    """Return existing proposal or create a new one for the pursuit."""
    existing = session.scalars(
        select(Proposal).where(Proposal.opportunity_id == opportunity_id)
    ).first()
    if existing is not None:
        return existing

    proposal = Proposal(
        opportunity_id=opportunity_id,
        pursuit_id=pursuit_id,
        status="draft",
    )
    session.add(proposal)
    session.flush()
    return proposal


def generate_proposal(
    session: Session,
    *,
    opportunity_id: int,
    actor: User | None = None,
    company_facts: dict[str, Any] | None = None,
    settings: Settings | None = None,
    skip_ai: bool = False,
) -> dict[str, Any]:
    """Generate an AI proposal draft for an approved-to-bid opportunity.

    Requires ``review_session.final_approval_status = approved_to_bid``.
    Creates a new ``ProposalVersion`` and runs coverage validation.

    When ``skip_ai=True`` (e.g. tests without an AI key), creates a
    placeholder draft with [[BLOCKER:...]] markers for every mandatory
    requirement that is not yet satisfied.
    """
    settings = settings or get_settings()

    # Verify the opportunity is approved to bid
    review = session.scalars(
        select(ReviewSession).where(ReviewSession.opportunity_id == opportunity_id)
    ).first()
    if review is None or review.final_approval_status != "approved_to_bid":
        raise ValueError(
            f"Opportunity {opportunity_id} is not approved_to_bid "
            f"(final_approval_status={getattr(review, 'final_approval_status', None)!r})"
        )

    opp = session.get(Opportunity, opportunity_id)
    if opp is None:
        raise ValueError(f"Opportunity {opportunity_id} not found")

    pursuit = session.scalars(
        select(Pursuit).where(Pursuit.opportunity_id == opportunity_id)
    ).first()
    if pursuit is None:
        raise ValueError(f"No pursuit record for opportunity {opportunity_id}")

    proposal = get_or_create_proposal(
        session, opportunity_id=opportunity_id, pursuit_id=pursuit.id
    )

    if skip_ai:
        draft_result = _build_placeholder_draft(session, opportunity_id)
        provider = "placeholder"
        model = None
    else:
        from govcon.ai.structured import StructuredCallError
        try:
            draft_result = draft_proposal(
                session,
                opportunity_id=opportunity_id,
                company_facts=company_facts,
                settings=settings,
            )
            provider = "deepseek"
            model = None
        except StructuredCallError as exc:
            logger.warning("AI drafting failed (%s); using placeholder draft: %s", exc.reason, exc.detail)
            draft_result = _build_placeholder_draft(session, opportunity_id)
            provider = "placeholder"
            model = None

    sections = _draft_result_to_sections(draft_result)
    global_blockers = draft_result.get("global_blockers", [])

    change_summary = f"AI-generated v1 draft from {len(sections)} section(s)"
    if global_blockers:
        change_summary += f"; {len(global_blockers)} global blocker(s)"

    pv = create_proposal_version(
        session,
        proposal_id=proposal.id,
        created_by=actor.email if actor else "system",
        sections=sections,
        provider=provider,
        model=model,
        change_summary=change_summary,
        metadata={
            "global_blockers": global_blockers,
            "draft_notes": draft_result.get("draft_notes", []),
            "source_fact_ids_used": draft_result.get("source_fact_ids_used", []),
        },
    )

    proposal.status = "ai_generated"
    session.flush()

    # Run coverage validation (deterministic pass only in this step)
    requirements = active_requirements(session, opportunity_id=opportunity_id)
    coverage = check_proposal_coverage(
        session,
        opportunity_id,
        pv.id,
        use_ai=False,
        settings=settings,
    )

    # Notify watchers
    _send_notification(
        session,
        opportunity_id=opportunity_id,
        actor=actor,
        notification_type="proposal_package_generated",
        payload={
            "proposal_id": proposal.id,
            "version_id": pv.id,
            "version_number": pv.version_number,
            "section_count": len(sections),
            "global_blockers": global_blockers,
        },
    )

    record_audit(
        session,
        user_id=actor.id if actor else None,
        opportunity_id=opportunity_id,
        action_type="proposal_generated",
        entity_type="proposal_version",
        entity_id=pv.id,
        new_value={"version_number": pv.version_number, "provider": provider},
    )

    return {
        "proposal_id": proposal.id,
        "version_id": pv.id,
        "version_number": pv.version_number,
        "section_count": len(sections),
        "global_blockers": global_blockers,
        "coverage_summary": coverage,
        "status": proposal.status,
    }


def _build_placeholder_draft(session: Session, opportunity_id: int) -> dict[str, Any]:
    """Build a minimal placeholder draft when AI is unavailable."""
    requirements = active_requirements(session, opportunity_id=opportunity_id)
    sections: list[dict[str, Any]] = []
    global_blockers: list[str] = []

    opp = session.get(Opportunity, opportunity_id)
    sol_num = (opp.solicitation_number or str(opportunity_id)) if opp else str(opportunity_id)

    for section_key in ["cover_letter", "executive_summary", "technical_response"]:
        reqs_for_section = [
            r for r in requirements
            if r.assigned_proposal_section == section_key
            or (section_key == "technical_response" and r.requirement_type == "technical")
        ]
        blockers_for_section = [
            f"[[BLOCKER: Requirement {r.id} not yet addressed — {r.requirement_text[:60]}...]]"
            for r in reqs_for_section
            if r.mandatory and r.status not in {"satisfied", "not_applicable"}
        ]
        sections.append({
            "section_key": section_key,
            "heading": {
                "cover_letter": "Cover / Quote Letter",
                "executive_summary": "Executive Summary",
                "technical_response": "Technical / Product Response",
            }.get(section_key, section_key),
            "content": "\n\n".join(blockers_for_section) if blockers_for_section
                       else f"[[BLOCKER: Section {section_key} requires human input — no AI provider configured]]",
            "requirement_ids": [r.id for r in reqs_for_section],
            "source_refs": None,
        })

    missing_mandatory = [
        r for r in requirements
        if r.mandatory and r.status not in {"satisfied", "not_applicable"}
    ]
    if missing_mandatory:
        global_blockers.append(
            f"[[BLOCKER: {len(missing_mandatory)} mandatory requirement(s) not satisfied]]"
        )

    return {
        "sections": [
            {
                "section_key": s["section_key"],
                "heading": s["heading"],
                "content": s["content"],
                "requirement_ids": s["requirement_ids"],
                "source_fact_ids": [],
                "blockers": [],
                "word_count": len(s["content"].split()),
            }
            for s in sections
        ],
        "global_blockers": global_blockers,
        "source_fact_ids_used": [],
        "draft_notes": ["placeholder draft — AI provider not configured"],
    }


def _draft_result_to_sections(draft_result: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert AI draft result to ProposalSection dicts."""
    sections: list[dict[str, Any]] = []
    for i, s in enumerate(draft_result.get("sections", [])):
        sections.append({
            "section_key": s.get("section_key") or f"section_{i}",
            "heading": s.get("heading"),
            "content": s.get("content") or "",
            "requirement_ids": s.get("requirement_ids") or [],
            "source_refs": {
                "source_fact_ids": s.get("source_fact_ids") or [],
                "blockers": s.get("blockers") or [],
            },
            "sort_order": i,
        })
    return sections


def get_proposal_workspace(
    session: Session,
    *,
    opportunity_id: int,
) -> dict[str, Any]:
    """Return the full proposal workspace for the final approver.

    Shows READY / NOT READY, requirement counts, blocking issues,
    coverage summary, submission destination, and deadline.
    """
    proposal = session.scalars(
        select(Proposal).where(Proposal.opportunity_id == opportunity_id)
    ).first()

    review = session.scalars(
        select(ReviewSession).where(ReviewSession.opportunity_id == opportunity_id)
    ).first()

    pursuit = session.scalars(
        select(Pursuit).where(Pursuit.opportunity_id == opportunity_id)
    ).first()

    submission = session.scalars(
        select(Submission).where(Submission.opportunity_id == opportunity_id)
    ).first()

    requirements = active_requirements(session, opportunity_id=opportunity_id)
    summary = coverage_summary(requirements)

    blocking_issues: list[str] = []
    # Check mandatory requirements
    mandatory_missing = sum(1 for r in requirements if r.mandatory and r.blocks_submission)
    if mandatory_missing > 0:
        blocking_issues.append(f"{mandatory_missing} mandatory requirement(s) block submission")

    current_version: ProposalVersion | None = None
    coverage_result: dict | None = None
    sections: list = []

    if proposal and proposal.current_version_id:
        current_version = session.get(ProposalVersion, proposal.current_version_id)
        sections = get_sections_for_version(session, proposal.current_version_id) if current_version else []

    readiness = "NOT_READY"
    if (
        proposal is not None
        and proposal.status in {"ai_generated", "red_teamed"}
        and mandatory_missing == 0
        and pursuit is not None
        and pursuit.stage not in {"cancelled", "no_bid"}
    ):
        readiness = "READY"

    return {
        "readiness": readiness,
        "opportunity_id": opportunity_id,
        "proposal_id": proposal.id if proposal else None,
        "proposal_status": proposal.status if proposal else None,
        "current_version_id": proposal.current_version_id if proposal else None,
        "current_version_number": current_version.version_number if current_version else None,
        "section_count": len(sections),
        "mandatory_total": summary.get("mandatory_total", 0),
        "mandatory_satisfied": summary.get("mandatory_satisfied", 0),
        "mandatory_missing": summary.get("mandatory_missing", 0),
        "mandatory_unknown": summary.get("mandatory_unknown", 0),
        "mandatory_needs_review": summary.get("mandatory_needs_review", 0),
        "critical_total": summary.get("critical_total", 0),
        "critical_unresolved": summary.get("critical_unresolved", 0),
        "blocking_issues": blocking_issues,
        "stale_count": sum(1 for r in requirements if r.stale_due_to_amendment),
        "unknown_count": sum(1 for r in requirements if r.status in {"unknown", "needs_review"}),
        "submission_destination": submission.submission_destination if submission else None,
        "submission_deadline": submission.submission_deadline.isoformat() if submission and submission.submission_deadline else None,
        "deadline_timezone": submission.deadline_timezone if submission else None,
        "review_approval_status": review.final_approval_status if review else None,
        "pursuit_stage": pursuit.stage if pursuit else None,
        "actions_available": ["APPROVE_FOR_SUBMISSION", "RETURN_FOR_FIX", "CANCEL_BID"],
    }


def finalize_proposal(
    session: Session,
    *,
    opportunity_id: int,
    action: str,
    actor: User,
    override_reason: str | None = None,
) -> dict[str, Any]:
    """Human final approval action: APPROVE_FOR_SUBMISSION | RETURN_FOR_FIX | CANCEL_BID.

    APPROVE_FOR_SUBMISSION:
      - sets Proposal.status = 'final_approved'
      - records approver and timestamp
      - calls move_to_ready_to_submit() on the pursuit
      - sets Submission.readiness_status = 'ready'

    RETURN_FOR_FIX:
      - sets Proposal.status = 'returned_for_fix'
      - records in audit

    CANCEL_BID:
      - sets Proposal.status = 'cancelled'
      - sets Pursuit.stage = 'cancelled'
    """
    require_permission(actor, "approve")

    proposal = session.scalars(
        select(Proposal).where(Proposal.opportunity_id == opportunity_id)
    ).first()
    if proposal is None:
        raise ValueError(f"No proposal for opportunity {opportunity_id}")

    pursuit = session.scalars(
        select(Pursuit).where(Pursuit.opportunity_id == opportunity_id)
    ).first()

    now = datetime.now(UTC)
    action_upper = action.upper()

    if action_upper == "APPROVE_FOR_SUBMISSION":
        proposal.status = "final_approved"
        proposal.final_approved_by_user_id = actor.id
        proposal.final_approved_at = now
        session.flush()

        # Move pursuit to ready_to_submit.
        # An override must be an explicit, audited human reason. Approval alone
        # cannot silently waive missing pre-flight evidence.
        try:
            move_to_ready_to_submit(
                session,
                opportunity_id=opportunity_id,
                actor=actor,
                override_reason=override_reason,
            )
        except ReadinessBlocked as exc:
            # Only hard/non-overridable blockers should propagate
            logger.warning(
                "Final approval blocked by %d hard blocker(s): %s",
                len(exc.blockers), exc.blockers,
            )
            raise

        # Mark submission ready
        submission = session.scalars(
            select(Submission).where(Submission.opportunity_id == opportunity_id)
        ).first()
        if submission is not None:
            submission.readiness_status = "ready"
            submission.status = "ready"
            session.flush()

        _send_notification(
            session,
            opportunity_id=opportunity_id,
            actor=actor,
            notification_type="submission_ready",
            payload={"proposal_id": proposal.id, "action": action_upper},
        )

    elif action_upper == "RETURN_FOR_FIX":
        proposal.status = "returned_for_fix"
        session.flush()

    elif action_upper == "CANCEL_BID":
        proposal.status = "cancelled"
        if pursuit is not None:
            pursuit.stage = "cancelled"
            session.flush()

    else:
        raise ValueError(f"Unknown action: {action!r}. Expected APPROVE_FOR_SUBMISSION | RETURN_FOR_FIX | CANCEL_BID")

    record_audit(
        session,
        user_id=actor.id,
        opportunity_id=opportunity_id,
        action_type=f"proposal_{action_upper.lower()}",
        entity_type="proposal",
        entity_id=proposal.id,
        new_value={"action": action_upper, "override_reason": override_reason},
    )

    return {
        "proposal_id": proposal.id,
        "status": proposal.status,
        "action": action_upper,
        "actor": actor.email,
        "at": now.isoformat(),
        "pursuit_stage": pursuit.stage if pursuit else None,
    }


def record_submission_confirmation(
    session: Session,
    *,
    opportunity_id: int,
    confirmation_number: str | None = None,
    confirmation_notes: str | None = None,
    actor: User,
) -> dict[str, Any]:
    """Record that the human has manually submitted and has a confirmation."""
    require_permission(actor, "approve")

    submission = session.scalars(
        select(Submission).where(Submission.opportunity_id == opportunity_id)
    ).first()
    if submission is None:
        raise ValueError(f"No submission record for opportunity {opportunity_id}")

    pursuit = session.scalars(
        select(Pursuit).where(Pursuit.opportunity_id == opportunity_id)
    ).first()

    now = datetime.now(UTC)
    submission.status = "submitted"
    submission.submitted_at = now
    if confirmation_number:
        submission.confirmation_number = confirmation_number
    if confirmation_notes:
        submission.notes = confirmation_notes
    session.flush()

    if pursuit is not None:
        pursuit.stage = "submitted"
        pursuit.submitted_at = now
        session.flush()

    record_audit(
        session,
        user_id=actor.id,
        opportunity_id=opportunity_id,
        action_type="submission_confirmed",
        entity_type="submission",
        entity_id=submission.id,
        new_value={
            "confirmation_number": confirmation_number,
            "notes": confirmation_notes,
        },
    )

    return {
        "submission_id": submission.id,
        "status": submission.status,
        "submitted_at": now.isoformat(),
        "confirmation_number": confirmation_number,
    }


def _send_notification(
    session: Session,
    *,
    opportunity_id: int,
    actor: User | None,
    notification_type: str,
    payload: dict[str, Any],
) -> None:
    """Send an in-app notification to the actor (best-effort)."""
    if actor is None:
        return
    notif = Notification(
        user_id=actor.id,
        opportunity_id=opportunity_id,
        notification_type=notification_type,
        payload=payload,
    )
    session.add(notif)
    session.flush()
