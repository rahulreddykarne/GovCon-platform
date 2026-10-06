"""Final submission checklist and step-by-step instructions (Phase 11).

Generates the human-readable final checklist and step-by-step submission
guide from the assembled compliance matrix and submission record.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.compliance.matrix import active_requirements
from govcon.compliance.metrics import coverage_summary, false_satisfied
from govcon.models import (
    Opportunity,
    Proposal,
    Submission,
)


def generate_final_checklist(
    session: Session,
    *,
    opportunity_id: int,
) -> dict[str, Any]:
    """Generate the final submission checklist.

    Returns a structured dict with items grouped by category. Each item has:
    - label: human-readable description
    - status: ready | pending | blocked | unknown
    - detail: optional detail string
    - requirement_ids: list of backing requirement IDs (if any)
    """
    submission = session.scalars(
        select(Submission).where(Submission.opportunity_id == opportunity_id)
    ).first()
    proposal = session.scalars(
        select(Proposal).where(Proposal.opportunity_id == opportunity_id)
    ).first()
    opp = session.get(Opportunity, opportunity_id)
    requirements = active_requirements(session, opportunity_id=opportunity_id)
    summary = coverage_summary(requirements)
    from govcon.submissions.manifest import current_package, verify_package
    from govcon.submissions.service import _extract_submission_info
    package = current_package(session, submission) if submission else None
    package_problems = verify_package(package) if package else ["No immutable assembled package manifest"]
    conflicts = _extract_submission_info(requirements)["destination_conflicts"]

    items: list[dict[str, Any]] = []
    items.append(_item("Package integrity", "blocked" if package_problems else "ready", "; ".join(package_problems)))
    if conflicts:
        items.append(_item("Submission destination conflict", "blocked", "Conflicting submission instructions require resolution"))

    # Proposal readiness
    if proposal is None:
        items.append(_item("Proposal document", "blocked", "No proposal generated yet"))
    elif proposal.status == "final_approved":
        items.append(_item("Proposal document", "ready", f"Proposal v{_current_version(session, proposal)} — final approved"))
    elif proposal.status in {"ai_generated", "red_teamed"}:
        items.append(_item("Proposal document", "pending", "Proposal exists but not yet final approved"))
    else:
        items.append(_item("Proposal document", "blocked", f"Proposal status: {proposal.status}"))

    # Mandatory coverage
    mandatory_missing = summary.get("mandatory_missing", 0)
    mandatory = [r for r in requirements if r.mandatory is not False and r.status not in {"not_applicable", "superseded"}]
    mandatory_unresolved = sum(r.status != "satisfied" or false_satisfied(r) for r in mandatory)
    if not requirements:
        items.append(_item("Mandatory requirements", "unknown", "No requirement inventory is available"))
    elif mandatory_unresolved == 0:
        items.append(_item(
            "Mandatory requirements",
            "ready",
            f"All {len(mandatory)} applicable mandatory requirements satisfied",
        ))
    else:
        items.append(_item(
            "Mandatory requirements",
            "blocked",
            f"{mandatory_unresolved} of {len(mandatory)} mandatory requirements not satisfied",
        ))

    # Critical requirements
    critical_unresolved = summary.get("critical_unresolved", 0)
    if critical_unresolved == 0:
        items.append(_item("Critical requirements", "ready", "No unresolved critical requirements"))
    else:
        items.append(_item(
            "Critical requirements",
            "blocked",
            f"{critical_unresolved} critical requirement(s) unresolved",
        ))

    # Stale requirements
    stale = [r for r in requirements if r.stale_due_to_amendment]
    if stale:
        items.append(_item(
            "Stale requirements",
            "blocked",
            f"{len(stale)} requirement(s) are stale due to amendments",
            requirement_ids=[r.id for r in stale],
        ))
    else:
        items.append(_item("Stale requirements", "ready", "No stale requirements"))

    # Submission method
    if submission and submission.submission_method:
        items.append(_item(
            "Submission method",
            "ready",
            f"Method: {submission.submission_method}",
        ))
    else:
        items.append(_item("Submission method", "unknown", "Submission method not determined"))

    # Destination
    dest = (submission.submission_destination or submission.recipient_email) if submission else None
    if dest:
        items.append(_item("Submission destination", "ready", f"Destination: {dest}"))
    else:
        items.append(_item("Submission destination", "unknown", "No destination determined"))

    # Deadline
    if submission and submission.submission_deadline:
        deadline_str = submission.submission_deadline.isoformat()
        tz = submission.deadline_timezone or "UTC"
        items.append(_item("Deadline", "ready", f"{deadline_str} ({tz})"))
    elif opp and opp.response_deadline:
        items.append(_item("Deadline", "ready", f"{opp.response_deadline.isoformat()} (from opportunity)"))
    else:
        items.append(_item("Deadline", "unknown", "No deadline found"))

    # Required files
    if submission and submission.required_files is not None:
        rf = submission.required_files
        files = rf.get("files", []) if isinstance(rf, dict) else (rf or [])
        from govcon.compliance.deterministic import required_files_present
        result = required_files_present(files, package) if package and files else None
        status = "ready" if not files and package else ("ready" if result and result.status == "pass" and not package_problems else "pending")
        items.append(_item("Required files", status, result.reason if result else f"{len(files)} required file(s) identified"))
    else:
        items.append(_item("Required files", "unknown", "Required file list not yet established"))

    # Amendment acknowledgments
    reqs_data = submission.required_actions or {} if submission else {}
    amendment_acks = reqs_data.get("amendment_acknowledgments", [])
    if amendment_acks:
        from govcon.compliance.deterministic import amendments_acknowledged
        result = amendments_acknowledged(amendment_acks, package)
        items.append(_item(
            "Amendment acknowledgments",
            "ready" if result.status == "pass" and not package_problems else "pending",
            result.reason,
        ))
    else:
        items.append(_item("Amendment acknowledgments", "ready", "No amendment acknowledgments required"))

    # Blocking submissions
    blocked_ids = [r.id for r in requirements if r.blocks_submission]
    if blocked_ids:
        items.append(_item(
            "Submission blockers",
            "blocked",
            f"{len(blocked_ids)} blocking requirement(s)",
            requirement_ids=blocked_ids,
        ))
    else:
        items.append(_item("Submission blockers", "ready", "No submission blockers"))

    overall = "ready" if all(i["status"] == "ready" for i in items) else (
        "blocked" if any(i["status"] == "blocked" for i in items) else "pending"
    )

    return {
        "opportunity_id": opportunity_id,
        "overall": overall,
        "items": items,
        "mandatory_total": summary.get("mandatory_total", 0),
        "mandatory_satisfied": len(mandatory) - mandatory_unresolved,
        "mandatory_missing": mandatory_missing,
        "mandatory_unresolved": mandatory_unresolved,
        "critical_unresolved": critical_unresolved,
        "stale_count": len(stale),
    }


def generate_step_by_step_instructions(
    session: Session,
    *,
    opportunity_id: int,
) -> list[dict[str, Any]]:
    """Generate step-by-step submission instructions from solicitation evidence."""
    submission = session.scalars(
        select(Submission).where(Submission.opportunity_id == opportunity_id)
    ).first()
    proposal = session.scalars(
        select(Proposal).where(Proposal.opportunity_id == opportunity_id)
    ).first()
    session.get(Opportunity, opportunity_id)

    steps: list[dict[str, Any]] = []
    checklist = generate_final_checklist(session, opportunity_id=opportunity_id)
    file_checks = [i for i in checklist["items"] if i["label"] in {"Required files", "Package integrity"}]

    steps.append({
        "step": 1,
        "title": "Verify final approved proposal",
        "description": (
            f"Confirm proposal v{_current_version(session, proposal)} is final-approved."
            if proposal and proposal.status == "final_approved"
            else "Generate and approve proposal before submitting."
        ),
        "status": "complete" if (proposal and proposal.status == "final_approved") else "pending",
    })

    steps.append({
        "step": 2,
        "title": "Assemble required files",
        "description": "Gather all required files per submission_checklist.json.",
        "status": "complete" if all(i["status"] == "ready" for i in file_checks) else "pending",
    })

    method = submission.submission_method if submission else None
    if submission is not None and method and "email" in method.lower():
        steps.append({
            "step": 3,
            "title": "Send submission email",
            "description": (
                f"Send to: {submission.recipient_email or 'unknown'}. "
                "Use the draft_submission_email output as a template."
            ),
            "status": "pending",
        })
    elif submission is not None and method and "portal" in method.lower():
        steps.append({
            "step": 3,
            "title": "Upload via portal",
            "description": (
                f"Upload files to: {(submission.portal_url or submission.portal_name or 'unknown')}. "
                "The platform does NOT auto-submit. Complete this step manually."
            ),
            "status": "pending",
        })
    else:
        steps.append({
            "step": 3,
            "title": "Submit per solicitation instructions",
            "description": "Submission method not yet determined. Review solicitation instructions.",
            "status": "unknown",
        })

    steps.append({
        "step": 4,
        "title": "Record confirmation",
        "description": "Run `govcon submission confirm <opportunity_id>` with confirmation number.",
        "status": "pending",
    })

    return steps


def _item(
    label: str,
    status: str,
    detail: str = "",
    requirement_ids: list[int] | None = None,
) -> dict[str, Any]:
    return {
        "label": label,
        "status": status,
        "detail": detail,
        "requirement_ids": requirement_ids or [],
    }


def _current_version(session: Session, proposal: Proposal | None) -> str:
    if proposal is None or proposal.current_version_id is None:
        return "?"
    from govcon.models import ProposalVersion
    pv = session.get(ProposalVersion, proposal.current_version_id)
    return str(pv.version_number) if pv else "?"
