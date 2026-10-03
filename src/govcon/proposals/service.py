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
from datetime import UTC, datetime, timedelta
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
    readiness_blockers,
)
from govcon.concurrency import StaleRecordError, apply_versioned_update
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
    ProposalWorkflowError,
    create_proposal_version,
    get_sections_for_version,
    latest_proposal_version,
)
from govcon.submissions.service import generate_submission_package
from govcon.workflow.invalidation import invalidate_submission_readiness, lock_one, lock_opportunity
from govcon.workflow.source_revision import current_source_revision, is_stale, stamp_of
from govcon.workflow.transitions import (
    APPROVABLE_PROPOSAL_STATUSES,
    InvalidTransition,
    require_transition,
)

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
    _generation_target(session, opportunity_id)

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
            provider = draft_result.get("provider")
            model = draft_result.get("model")
        except StructuredCallError as exc:
            logger.warning("AI drafting failed (%s); using placeholder draft: %s", exc.reason, exc.detail)
            draft_result = _build_placeholder_draft(session, opportunity_id)
            provider = "placeholder"
            model = None

    return publish_generated_proposal(
        session, opportunity_id=opportunity_id, actor=actor, draft_result=draft_result,
        provider=provider, model=model, settings=settings,
    )


def _generation_target(session: Session, opportunity_id: int) -> Proposal:
    """The proposal to draft into, after checking the bid is approved and open."""
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
    if proposal.status == "cancelled":
        raise ProposalWorkflowError(f"proposal for opportunity {opportunity_id} was cancelled")
    return proposal


def publish_generated_proposal(
    session: Session,
    *,
    opportunity_id: int,
    actor: User | None,
    draft_result: dict[str, Any],
    provider: str | None,
    model: str | None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Store a finished draft as a new version, check coverage, notify and audit.

    Shared by synchronous generation and the durable proposal task, which
    drafts with no transaction open and publishes here under the opportunity lock.
    """
    settings = settings or get_settings()
    proposal = _generation_target(session, opportunity_id)
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
        status_after="ai_generated",
        actor_id=actor.id if actor else None,
    )
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
        # What the compliance gate would refuse; final approval shows these to the approver.
        "readiness_blockers": readiness_blockers(session, opportunity_id),
        "proposal_version": proposal.version if proposal else None,
        "approved_version_id": proposal.approved_version_id if proposal else None,
    }


def finalize_proposal(
    session: Session,
    *,
    opportunity_id: int,
    action: str,
    actor: User,
    expected_version: int,
    override_reason: str | None = None,
) -> dict[str, Any]:
    """Human final approval action: APPROVE_FOR_SUBMISSION | RETURN_FOR_FIX | CANCEL_BID.

    The caller must hold ``approve`` and pass the proposal version it read.

    APPROVE_FOR_SUBMISSION:
      - allowed only from ``ai_generated`` or ``red_teamed`` on a bid that is
        still ``approved_to_bid``, for a version written against the current
        source revision;
      - runs ``move_to_ready_to_submit`` first. Blockers refuse the approval
        unless the caller passes their own override reason (and holds
        ``override_compliance``); nothing changes when the gate refuses;
      - then pins ``approved_version_id``, sets ``final_approved``, and marks
        the submission ``ready``.

    RETURN_FOR_FIX: ``returned_for_fix``; a prior approval's readiness lapses.

    CANCEL_BID: proposal ``cancelled``, pursuit ``cancelled``, an unsubmitted
    submission ``withdrawn``.
    """
    require_permission(actor, "approve")
    action_upper = action.upper()
    if action_upper not in {"APPROVE_FOR_SUBMISSION", "RETURN_FOR_FIX", "CANCEL_BID"}:
        raise ValueError(f"Unknown action: {action!r}. Expected APPROVE_FOR_SUBMISSION | RETURN_FOR_FIX | CANCEL_BID")
    if expected_version is None:
        raise ProposalWorkflowError("expected_version is required for final proposal decisions")

    lock_opportunity(session, opportunity_id)  # opportunity first, then proposal, pursuit, submission
    proposal = lock_one(session, select(Proposal).where(Proposal.opportunity_id == opportunity_id))
    if proposal is None:
        raise ValueError(f"No proposal for opportunity {opportunity_id}")
    if proposal.version != expected_version:
        raise ProposalWorkflowError(
            f"{StaleRecordError(proposal.version, expected_version)}; reload the proposal before deciding"
        )
    pursuit = lock_one(session, select(Pursuit).where(Pursuit.opportunity_id == opportunity_id))
    now = datetime.now(UTC)
    old_status = proposal.status

    if action_upper == "APPROVE_FOR_SUBMISSION":
        if proposal.status not in APPROVABLE_PROPOSAL_STATUSES:
            raise ProposalWorkflowError(
                f"a {proposal.status!r} proposal cannot be approved; "
                f"approval requires one of {sorted(APPROVABLE_PROPOSAL_STATUSES)}"
            )
        if proposal.current_version_id is None:
            raise ProposalWorkflowError("the proposal has no version to approve")
        review = session.scalar(select(ReviewSession).where(ReviewSession.opportunity_id == opportunity_id))
        if review is None or review.final_approval_status != "approved_to_bid":
            raise ProposalWorkflowError("the bid is not approved_to_bid; the review must approve it first")
        current = session.get(ProposalVersion, proposal.current_version_id)
        if current is not None and is_stale(
            stamp_of(current.version_metadata), current_source_revision(session, opportunity_id)
        ):
            raise ProposalWorkflowError(
                f"proposal version {current.version_number} was written against a superseded "
                "source revision; create a new version before approving"
            )
        # Gate first. ReadinessBlocked propagates and nothing below runs.
        move_to_ready_to_submit(
            session,
            opportunity_id=opportunity_id,
            actor=actor,
            override_reason=override_reason,
        )
        apply_versioned_update(
            session,
            proposal,
            expected_version,
            {
                "status": "final_approved",
                "final_approved_by_user_id": actor.id,
                "final_approved_at": now,
                "approved_version_id": proposal.current_version_id,
            },
        )
        submission = _latest_submission(session, opportunity_id)
        if submission is None:
            generate_submission_package(session, opportunity_id=opportunity_id, actor=actor)
            submission = _latest_submission(session, opportunity_id)
        if submission is not None:
            if submission.status in {"failed"}:
                require_transition("submission", submission.status, "ready")
            if submission.status in {"preparing", "failed"}:
                submission.status = "ready"
            submission.readiness_status = "ready"
            submission.version = (submission.version or 1) + 1
            session.flush()
        _send_notification(
            session,
            opportunity_id=opportunity_id,
            actor=actor,
            notification_type="submission_ready",
            payload={"proposal_id": proposal.id, "action": action_upper, "approved_version_id": proposal.approved_version_id},
        )

    elif action_upper == "RETURN_FOR_FIX":
        try:
            require_transition("proposal", proposal.status, "returned_for_fix")
        except InvalidTransition as exc:
            raise ProposalWorkflowError(str(exc)) from exc
        if proposal.status == "final_approved":
            invalidate_submission_readiness(
                session, opportunity_id, reason="proposal returned for fix", actor_id=actor.id
            )
        apply_versioned_update(
            session,
            proposal,
            expected_version,
            {
                "status": "returned_for_fix",
                "approved_version_id": None,
                "final_approved_by_user_id": None,
                "final_approved_at": None,
            },
        )

    else:  # CANCEL_BID
        try:
            require_transition("proposal", proposal.status, "cancelled")
            if pursuit is not None:
                require_transition("pursuit", pursuit.stage, "cancelled")
        except InvalidTransition as exc:
            raise ProposalWorkflowError(str(exc)) from exc
        apply_versioned_update(session, proposal, expected_version, {"status": "cancelled"})
        if pursuit is not None and pursuit.stage != "cancelled":
            apply_versioned_update(session, pursuit, pursuit.version, {"stage": "cancelled"})
        submission = _latest_submission(session, opportunity_id)
        if submission is not None and submission.status in {"preparing", "ready", "failed"}:
            apply_versioned_update(session, submission, submission.version, {"status": "withdrawn"})

    record_audit(
        session,
        user_id=actor.id,
        opportunity_id=opportunity_id,
        action_type=f"proposal_{action_upper.lower()}",
        entity_type="proposal",
        entity_id=proposal.id,
        old_value={"status": old_status, "version": expected_version},
        new_value={
            "action": action_upper,
            "status": proposal.status,
            "override_reason": override_reason,
            "approved_version_id": proposal.approved_version_id,
        },
    )

    return {
        "proposal_id": proposal.id,
        "status": proposal.status,
        "action": action_upper,
        "actor": actor.email,
        "at": now.isoformat(),
        "pursuit_stage": pursuit.stage if pursuit else None,
        "approved_version_id": proposal.approved_version_id,
        "version": proposal.version,
    }


def record_submission_confirmation(
    session: Session,
    *,
    opportunity_id: int,
    actor: User,
    expected_version: int,
    confirmation_number: str | None = None,
    confirmation_notes: str | None = None,
    submitted_at: datetime | None = None,
) -> dict[str, Any]:
    """Record that a human manually submitted the approved package.

    Requires ``approve``, the submission version the caller read, a
    final-approved proposal pinned to the current version, a pursuit in
    ``ready_to_submit`` (reached only through the compliance gate), a submission
    in ``ready``, confirmation evidence (a confirmation number, or notes of at
    least 10 characters), and a submission time not after the deadline.
    Repeating the call after it succeeded changes nothing.
    """
    require_permission(actor, "approve")
    lock_opportunity(session, opportunity_id)
    submission = lock_one(
        session,
        select(Submission)
        .where(Submission.opportunity_id == opportunity_id)
        .order_by(Submission.id.desc())
        .limit(1),
    )
    if submission is None:
        raise ValueError(f"No submission record for opportunity {opportunity_id}")
    if submission.status in {"submitted", "confirmed"}:
        return {
            "submission_id": submission.id,
            "status": submission.status,
            "submitted_at": submission.submitted_at.isoformat() if submission.submitted_at else None,
            "confirmation_number": submission.confirmation_number,
            "already_recorded": True,
        }
    if expected_version is None:
        raise ProposalWorkflowError("expected_version is required to record a submission")
    if submission.version != expected_version:
        raise ProposalWorkflowError(
            f"{StaleRecordError(submission.version, expected_version)}; reload the submission"
        )

    proposal = session.scalar(select(Proposal).where(Proposal.opportunity_id == opportunity_id))
    if proposal is None or proposal.status != "final_approved":
        raise ProposalWorkflowError("the proposal must be final_approved before a submission is recorded")
    if proposal.approved_version_id is None or proposal.approved_version_id != proposal.current_version_id:
        raise ProposalWorkflowError("the approved proposal version is not the current version; re-approve it")
    pursuit = lock_one(session, select(Pursuit).where(Pursuit.opportunity_id == opportunity_id))
    if pursuit is None or pursuit.stage != "ready_to_submit":
        raise ProposalWorkflowError(
            f"pursuit must be ready_to_submit (currently {pursuit.stage if pursuit else None!r}); "
            "run final approval and the compliance gate first"
        )
    if submission.status != "ready":
        raise ProposalWorkflowError(f"submission is {submission.status!r}, not ready")
    number = (confirmation_number or "").strip() or None
    notes = (confirmation_notes or "").strip() or None
    if number is None and (notes is None or len(notes) < 10):
        raise ProposalWorkflowError(
            "confirmation evidence is required: a portal/email confirmation number, "
            "or notes (10+ characters) describing the proof of submission"
        )
    now = datetime.now(UTC)
    when = submitted_at or now
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    if when > now + timedelta(minutes=5):
        raise ProposalWorkflowError("submission time cannot be in the future")
    opportunity = session.get(Opportunity, opportunity_id)
    deadline = submission.submission_deadline or (opportunity.response_deadline if opportunity else None)
    if deadline is not None:
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        if when > deadline:
            raise ProposalWorkflowError(
                f"the recorded submission time {when.isoformat()} is after the response deadline {deadline.isoformat()}"
            )

    from govcon.submissions.manifest import current_package, manifest_hash, proposal_artifact_problems, verify_package
    from govcon.compliance.matrix import latest_run
    package = current_package(session, submission)
    if package is None or verify_package(package):
        raise ProposalWorkflowError("the assembled submission package is missing or changed; re-run pre-flight")
    artifact_problems = proposal_artifact_problems(session, opportunity_id, package)
    if artifact_problems:
        raise ProposalWorkflowError("; ".join(artifact_problems))
    preflight = latest_run(session, opportunity_id, "submission_preflight")
    if preflight is None or manifest_hash(preflight.output_json.get("package") or {}) != submission.package_manifest_hash:
        raise ProposalWorkflowError("the assembled package is not the package checked by pre-flight")
    if package.proposal_version_id != proposal.approved_version_id:
        raise ProposalWorkflowError("the assembled package does not contain the approved proposal version")
    from govcon.compliance.submission_preflight import readiness_blockers
    from govcon.models import AuditEvent
    blockers = readiness_blockers(session, opportunity_id)
    if blockers:
        overrides = session.scalars(select(AuditEvent).where(AuditEvent.opportunity_id == opportunity_id, AuditEvent.action_type == "compliance_readiness_override").order_by(AuditEvent.id.desc())).all()
        if not any(
            (event.new_value or {}).get("package_manifest_sha256") == submission.package_manifest_hash
            and (event.new_value or {}).get("preflight_run_id") == preflight.id
            and (event.old_value or {}).get("blockers") == blockers
            for event in overrides
        ):
            raise ProposalWorkflowError("submission readiness changed after approval; re-run pre-flight and approval")
    try:
        require_transition("submission", submission.status, "submitted")
        require_transition("pursuit", pursuit.stage, "submitted")
    except InvalidTransition as exc:
        raise ProposalWorkflowError(str(exc)) from exc
    updates: dict[str, Any] = {"status": "submitted", "submitted_at": when, "submitted_files": package.manifest()}
    if number:
        updates["confirmation_number"] = number
    if notes:
        updates["notes"] = notes
    apply_versioned_update(session, submission, expected_version, updates)
    apply_versioned_update(session, pursuit, pursuit.version, {"stage": "submitted", "submitted_at": when})

    record_audit(
        session,
        user_id=actor.id,
        opportunity_id=opportunity_id,
        action_type="submission_confirmed",
        entity_type="submission",
        entity_id=submission.id,
        new_value={
            "confirmation_number": number,
            "notes": notes,
            "submitted_at": when.isoformat(),
            "approved_version_id": proposal.approved_version_id,
        },
    )

    return {
        "submission_id": submission.id,
        "status": submission.status,
        "submitted_at": when.isoformat(),
        "confirmation_number": number,
        "already_recorded": False,
    }


def _send_notification(
    session: Session,
    *,
    opportunity_id: int,
    actor: User | None,
    notification_type: str,
    payload: dict[str, Any],
) -> None:
    """Notify the acting user plus every active owner/approver (once each)."""
    recipients: dict[int, None] = {}
    if actor is not None:
        recipients[actor.id] = None
    for user in session.scalars(
        select(User).where(User.is_active.is_(True), User.role.in_(["owner", "approver"]))
    ).all():
        recipients[user.id] = None
    for user_id in recipients:
        session.add(
            Notification(
                user_id=user_id,
                opportunity_id=opportunity_id,
                notification_type=notification_type,
                payload=payload,
            )
        )
    session.flush()


def _latest_submission(session: Session, opportunity_id: int) -> Submission | None:
    return session.scalar(
        select(Submission)
        .where(Submission.opportunity_id == opportunity_id)
        .order_by(Submission.id.desc())
        .limit(1)
    )


