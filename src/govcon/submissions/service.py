"""Submission package generation and management (Phase 11).

Generates the structured ``Submission`` record from solicitation evidence
extracted by the compliance pipeline. The platform assembles the package and
generates instructions; the human must submit manually.

v1 policy: the platform may NOT autonomously click through or submit into
arbitrary government portals.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.audit import record_audit
from govcon.compliance.matrix import active_requirements
from govcon.config import Settings, get_settings
from govcon.models import (
    Opportunity,
    Proposal,
    ProposalVersion,
    Pursuit,
    Requirement,
    Submission,
    User,
)

logger = logging.getLogger("govcon.submissions.service")


def _extract_submission_info(requirements: list[Requirement]) -> dict[str, Any]:
    """Aggregate submission-related key_values from requirements."""
    kv_list = [r.key_values or {} for r in requirements]

    methods: list[str] = []
    portals: list[str] = []
    portal_urls: list[str] = []
    recipients: list[str] = []
    timezones: list[str] = []
    forms: list[str] = []
    required_files: list[str] = []
    required_filenames: list[str] = []
    allowed_file_types: list[str] = []
    amendment_acks: list[str] = []
    max_file_size_mb: float | None = None
    page_limit: int | None = None
    deadline: str | None = None

    for kv in kv_list:
        if kv.get("submission_method"):
            methods.append(kv["submission_method"])
        if kv.get("submission_portal"):
            portals.append(kv["submission_portal"])
        if kv.get("portal_url"):
            portal_urls.append(kv["portal_url"])
        if kv.get("recipient_email"):
            recipients.append(kv["recipient_email"])
        if kv.get("deadline_timezone"):
            timezones.append(kv["deadline_timezone"])
        if kv.get("forms"):
            forms.extend(kv["forms"])
        if kv.get("required_files"):
            required_files.extend(kv["required_files"])
        if kv.get("required_filenames"):
            required_filenames.extend(kv["required_filenames"])
        if kv.get("allowed_file_types"):
            allowed_file_types.extend(kv["allowed_file_types"])
        if kv.get("amendment_acknowledgments"):
            amendment_acks.extend(kv["amendment_acknowledgments"])
        if kv.get("max_file_size_mb") is not None:
            v = float(kv["max_file_size_mb"])
            if max_file_size_mb is None or v < max_file_size_mb:
                max_file_size_mb = v
        if kv.get("page_limit") is not None:
            v = int(kv["page_limit"])
            if page_limit is None or v < page_limit:
                page_limit = v
        if kv.get("deadline"):
            deadline = kv["deadline"]

    return {
        "submission_method": methods[0] if len(set(methods)) == 1 else (methods[0] if methods else None),
        "submission_destination": portals[0] if len(set(portals)) == 1 else (portals[0] if portals else None),
        "portal_name": portals[0] if portals else None,
        "portal_url": portal_urls[0] if portal_urls else None,
        "recipient_email": recipients[0] if len(set(recipients)) == 1 else (recipients[0] if recipients else None),
        "deadline_timezone": timezones[0] if timezones else None,
        "forms": sorted(set(forms)),
        "required_files": sorted(set(required_files + forms)),
        "required_filenames": sorted(set(required_filenames)),
        "allowed_file_types": sorted(set(allowed_file_types)),
        "amendment_acknowledgments": sorted(set(amendment_acks)),
        "max_file_size_mb": max_file_size_mb,
        "page_limit": page_limit,
        "extracted_deadline": deadline,
    }


def generate_submission_package(
    session: Session,
    *,
    opportunity_id: int,
    actor: User | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Generate or refresh the Submission record for an opportunity.

    Extracts submission instructions from the compliance matrix and updates
    the Submission row. Creates a new Submission if none exists.

    Returns a summary dict of the submission package state.
    """
    settings = settings or get_settings()

    opp = session.get(Opportunity, opportunity_id)
    if opp is None:
        raise ValueError(f"Opportunity {opportunity_id} not found")

    pursuit = session.scalars(
        select(Pursuit).where(Pursuit.opportunity_id == opportunity_id)
    ).first()
    if pursuit is None:
        raise ValueError(f"No pursuit for opportunity {opportunity_id}")

    requirements = active_requirements(session, opportunity_id=opportunity_id)
    info = _extract_submission_info(requirements)

    submission = session.scalars(
        select(Submission).where(Submission.opportunity_id == opportunity_id)
    ).first()

    if submission is None:
        submission = Submission(
            opportunity_id=opportunity_id,
            pursuit_id=pursuit.id,
            status="preparing",
        )
        session.add(submission)
        session.flush()

    # Update from extracted info (only when not already manually set)
    if not submission.submission_method and info["submission_method"]:
        submission.submission_method = info["submission_method"]
    if not submission.submission_destination and info["submission_destination"]:
        submission.submission_destination = info["submission_destination"]
    if not submission.portal_name and info["portal_name"]:
        submission.portal_name = info["portal_name"]
    if not submission.portal_url and info["portal_url"]:
        submission.portal_url = info["portal_url"]
    if not submission.recipient_email and info["recipient_email"]:
        submission.recipient_email = info["recipient_email"]
    if not submission.deadline_timezone and info["deadline_timezone"]:
        submission.deadline_timezone = info["deadline_timezone"]
    if not submission.submission_deadline:
        # Use parsed deadline from requirements if available, else opportunity deadline
        submission.submission_deadline = opp.response_deadline

    # Merge required files
    existing_files = submission.required_files
    if isinstance(existing_files, dict):
        existing_file_list = existing_files.get("files", [])
    elif isinstance(existing_files, list):
        existing_file_list = existing_files
    else:
        existing_file_list = []
    merged_files = sorted(set(existing_file_list) | set(info["required_files"]))
    submission.required_files = {"files": merged_files}

    # Store required_actions: forms, filenames, amendment acks
    submission.required_actions = {
        "forms": info["forms"],
        "required_filenames": info["required_filenames"],
        "amendment_acknowledgments": info["amendment_acknowledgments"],
        "allowed_file_types": info["allowed_file_types"],
        "max_file_size_mb": info["max_file_size_mb"],
        "page_limit": info["page_limit"],
    }

    session.flush()

    record_audit(
        session,
        user_id=actor.id if actor else None,
        opportunity_id=opportunity_id,
        action_type="submission_package_generated",
        entity_type="submission",
        entity_id=submission.id,
        new_value={"required_files": merged_files},
    )

    missing_docs = _identify_missing_documents(opp, submission, requirements)

    return {
        "submission_id": submission.id,
        "status": submission.status,
        "submission_method": submission.submission_method,
        "submission_destination": submission.submission_destination,
        "portal_name": submission.portal_name,
        "portal_url": submission.portal_url,
        "recipient_email": submission.recipient_email,
        "deadline": submission.submission_deadline.isoformat() if submission.submission_deadline else None,
        "deadline_timezone": submission.deadline_timezone,
        "required_files": merged_files,
        "forms": info["forms"],
        "amendment_acknowledgments": info["amendment_acknowledgments"],
        "missing_documents": missing_docs,
        "readiness_status": submission.readiness_status,
    }


def _identify_missing_documents(
    opp: Opportunity,
    submission: Submission,
    requirements: list[Requirement],
) -> list[str]:
    """Identify mandatory documents not yet assembled."""
    missing: list[str] = []
    for req in requirements:
        if req.requirement_type in {"signature", "amendment_acknowledgment"} and req.mandatory:
            if req.status not in {"satisfied", "not_applicable"}:
                missing.append(f"Requirement {req.id}: {req.requirement_text[:80]}")
    if not submission.submission_method:
        missing.append("Submission method not determined")
    if not submission.submission_destination and not submission.recipient_email:
        missing.append("Submission destination not determined")
    return missing


def get_submission_workspace(
    session: Session,
    *,
    opportunity_id: int,
) -> dict[str, Any]:
    """Return the full submission workspace state."""
    submission = session.scalars(
        select(Submission).where(Submission.opportunity_id == opportunity_id)
    ).first()
    proposal = session.scalars(
        select(Proposal).where(Proposal.opportunity_id == opportunity_id)
    ).first()
    requirements = active_requirements(session, opportunity_id=opportunity_id)

    missing = _identify_missing_documents(
        session.get(Opportunity, opportunity_id),
        submission,
        requirements,
    ) if submission else ["No submission record — run generate first"]

    return {
        "submission_id": submission.id if submission else None,
        "status": submission.status if submission else None,
        "readiness_status": submission.readiness_status if submission else None,
        "required_files": (
            (submission.required_files.get("files", []) if isinstance(submission.required_files, dict)
             else submission.required_files or []) if submission else []
        ),
        "required_actions": submission.required_actions if submission else {},
        "missing_documents": missing,
        "proposal_status": proposal.status if proposal else None,
        "submission_method": submission.submission_method if submission else None,
        "destination": submission.submission_destination if submission else None,
        "recipient_email": submission.recipient_email if submission else None,
        "deadline": submission.submission_deadline.isoformat() if submission and submission.submission_deadline else None,
        "deadline_timezone": submission.deadline_timezone if submission else None,
    }
