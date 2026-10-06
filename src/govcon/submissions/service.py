"""Submission package generation and management (Phase 11).

Generates the structured ``Submission`` record from solicitation evidence
extracted by the compliance pipeline. The platform assembles the package and
generates instructions; the human must submit manually.

v1 policy: the platform may NOT autonomously click through or submit into
arbitrary government portals.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.audit import record_audit
from govcon.compliance.matrix import (
    active_requirements,
    close_undetected_findings,
    record_run,
    upsert_open_finding,
)
from govcon.config import Settings, get_settings
from govcon.models import (
    Proposal,
    Pursuit,
    Requirement,
    Submission,
    User,
)
from govcon.workflow.invalidation import (
    invalidate_submission_readiness,
    lock_one,
    lock_opportunity,
)

logger = logging.getLogger("govcon.submissions.service")

# Spellings of one US time zone; different zones are a conflict.
_TIMEZONE_ALIASES = {
    "EST": "ET", "EDT": "ET", "EASTERN": "ET",
    "CST": "CT", "CDT": "CT", "CENTRAL": "CT",
    "MST": "MT", "MDT": "MT", "MOUNTAIN": "MT",
    "PST": "PT", "PDT": "PT", "PACIFIC": "PT",
    "GMT": "UTC", "Z": "UTC",
}
_DATE_FORMATS = ("%B %d %Y", "%b %d %Y", "%Y-%m-%d", "%m/%d/%Y")
# A submission in these states keeps the instructions it was made under.
FROZEN_SUBMISSION_STATUSES = frozenset({"submitted", "confirmed", "withdrawn"})


def _parse_date(value: str | None) -> date | None:
    """Calendar date from an extracted deadline string; None when unparseable."""
    raw = (value or "").strip()
    for candidate in (raw, raw.split(" ")[0]):
        try:
            return datetime.fromisoformat(candidate).date()
        except ValueError:
            continue
    text = re.sub(r"\s+", " ", raw.replace(",", " ").replace(".", " ")).strip()
    if not text:
        return None
    words = text.split(" ")
    for candidate in (" ".join(words[:3]), words[0]):
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
    return None


def _audit_value(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _deadline_disagrees(deadline: datetime | None, extracted: list[str]) -> bool:
    """True when an extracted date cannot be the opportunity deadline in any time zone."""
    if deadline is None or not extracted:
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    possible = {(deadline + timedelta(hours=hours)).date() for hours in (-12, 0, 14)}
    return any(date.fromisoformat(value) not in possible for value in extracted)


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
    deadlines: list[str] = []

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
            # The extracted response deadline. ``response_deadline_date`` is not
            # used here: it also matches question and delivery due dates.
            deadline = kv["deadline"]
            deadlines.append(str(kv["deadline"]))

    values = {"submission_method": methods, "submission_destination": portals, "portal_url": portal_urls, "recipient_email": recipients}
    normalized = {k: sorted({str(v).strip().lower() if k in {"submission_method", "recipient_email"} else str(v).strip().rstrip("/") for v in vs}) for k, vs in values.items()}
    conflicts = {k: vs for k, vs in normalized.items() if len(vs) > 1}
    destination = {k: vs[0] if len(vs) == 1 and not conflicts else None for k, vs in normalized.items()}
    deadline_conflicts: dict[str, list[str]] = {}
    zones = {_TIMEZONE_ALIASES.get(str(t).strip().upper(), str(t).strip().upper()) for t in timezones}
    if len(zones) > 1:
        deadline_conflicts["deadline_timezone"] = sorted({str(t).strip() for t in timezones})
    dates = sorted({d for d in (_parse_date(v) for v in deadlines) if d is not None})
    if len(dates) > 1:
        deadline_conflicts["deadline"] = [d.isoformat() for d in dates]
    return {
        **destination,
        "destination_conflicts": conflicts,
        "deadline_conflicts": deadline_conflicts,
        "extracted_deadline_dates": [d.isoformat() for d in dates],
        "portal_name": destination["submission_destination"],
        "deadline_timezone": None if "deadline_timezone" in deadline_conflicts else (timezones[0] if timezones else None),
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

    Every instruction (method, destination, recipient, deadline, time zone,
    required files) is recomputed from the current source, so a changed
    solicitation never leaves a superseded value in place. The deadline is the
    opportunity's response deadline; extracted deadlines or time zones that
    disagree become a blocking ``submission_deadline_conflict`` finding
    instead of one being chosen silently. A change to an existing record is
    audited and invalidates submission readiness. A submitted or withdrawn
    record is left as it was.

    Returns a summary dict of the submission package state.
    """
    settings = settings or get_settings()

    opp = lock_opportunity(session, opportunity_id)
    if opp is None:
        raise ValueError(f"Opportunity {opportunity_id} not found")

    pursuit = lock_one(session,
        select(Pursuit).where(Pursuit.opportunity_id == opportunity_id)
    )
    if pursuit is None:
        raise ValueError(f"No pursuit for opportunity {opportunity_id}")

    requirements = active_requirements(session, opportunity_id=opportunity_id)
    info = _extract_submission_info(requirements)
    destination_run = record_run(session, opportunity_id=opportunity_id, run_type="submission_destination", run_version="v1", output=info)
    kept: set[int] = set()
    if info["destination_conflicts"]:
        finding = upsert_open_finding(session, opportunity_id=opportunity_id, finding_type="submission_destination_conflict", severity="critical", description="Conflicting submission destinations or methods require human resolution", detected_by="submission_destination", detector_version="v1", source_refs=info["destination_conflicts"], blocks_submission=True)
        kept.add(finding.id)
    deadline_conflicts = dict(info["deadline_conflicts"])
    if opp.response_deadline is not None and _deadline_disagrees(opp.response_deadline, info["extracted_deadline_dates"]):
        deadline_conflicts["opportunity_deadline"] = [opp.response_deadline.isoformat(), *info["extracted_deadline_dates"]]
    if deadline_conflicts:
        finding = upsert_open_finding(
            session,
            opportunity_id=opportunity_id,
            finding_type="submission_deadline_conflict",
            severity="critical",
            description="Conflicting response deadlines or deadline time zones in the solicitation: "
            + "; ".join(f"{k}: {', '.join(map(str, v))}" for k, v in sorted(deadline_conflicts.items())),
            detected_by="submission_destination",
            detector_version="v1",
            source_refs=deadline_conflicts,
            blocks_submission=True,
        )
        kept.add(finding.id)
    close_undetected_findings(session, opportunity_id, detected_by="submission_destination", keep_ids=kept, run_id=destination_run.id)

    submission = session.scalars(
        select(Submission).where(Submission.opportunity_id == opportunity_id)
    ).first()

    created = submission is None
    if submission is None:
        submission = Submission(
            opportunity_id=opportunity_id,
            pursuit_id=pursuit.id,
            status="preparing",
        )
        session.add(submission)
        session.flush()
    elif submission.status in FROZEN_SUBMISSION_STATUSES:
        # The instructions a submission was made under are part of its record.
        logger.info("submission %s is %s; instructions are not regenerated", submission.id, submission.status)
        return _summary(submission, info, list(_file_list(submission.required_files) or []), _identify_missing_documents(submission, requirements))

    # Every instruction is recomputed from the current source: a value kept
    # from an earlier solicitation revision would let pre-flight validate
    # superseded instructions. Conflicting values stay empty (unknown).
    derived = {
        "submission_method": info["submission_method"],
        "submission_destination": info["submission_destination"],
        "portal_name": info["portal_name"],
        "portal_url": info["portal_url"],
        "recipient_email": info["recipient_email"],
        "deadline_timezone": info["deadline_timezone"],
        "submission_deadline": opp.response_deadline,
    }
    old_file_list = _file_list(submission.required_files)
    required_files = list(info["required_files"])
    changed = {
        field: {"old": _audit_value(getattr(submission, field)), "new": _audit_value(value)}
        for field, value in derived.items()
        if getattr(submission, field) != value
    }
    if old_file_list is None or sorted(old_file_list) != required_files:
        changed["required_files"] = {"old": old_file_list, "new": required_files}
    for field, value in derived.items():
        setattr(submission, field, value)
    submission.required_files = {"files": required_files}

    # Store required_actions: forms, filenames, amendment acks
    submission.required_actions = {
        "forms": info["forms"],
        "required_filenames": info["required_filenames"],
        "amendment_acknowledgments": info["amendment_acknowledgments"],
        "allowed_file_types": info["allowed_file_types"],
        "max_file_size_mb": info["max_file_size_mb"],
        "page_limit": info["page_limit"],
        "deadline_conflicts": deadline_conflicts,
    }
    if info["destination_conflicts"] or deadline_conflicts:
        submission.readiness_status = "not_ready"
    if changed and not created:
        submission.version = (submission.version or 1) + 1
        # A pre-flight that checked the previous instructions no longer applies.
        invalidate_submission_readiness(session, opportunity_id, reason="submission_instructions_changed", actor_id=actor.id if actor else None)

    session.flush()

    record_audit(
        session,
        user_id=actor.id if actor else None,
        opportunity_id=opportunity_id,
        action_type="submission_package_generated",
        entity_type="submission",
        entity_id=submission.id,
        new_value={"required_files": required_files, "changed": changed, "deadline_conflicts": deadline_conflicts},
    )
    return _summary(submission, info, required_files, _identify_missing_documents(submission, requirements))


def _file_list(raw: Any) -> list[str] | None:
    if isinstance(raw, dict):
        return [str(f) for f in raw.get("files", [])]
    if isinstance(raw, list):
        return [str(f) for f in raw]
    return None


def _summary(submission: Submission, info: dict[str, Any], required_files: list[str], missing_docs: list[str]) -> dict[str, Any]:
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
        "required_files": required_files,
        "forms": info["forms"],
        "amendment_acknowledgments": info["amendment_acknowledgments"],
        "missing_documents": missing_docs,
        "destination_conflicts": info["destination_conflicts"],
        "deadline_conflicts": (submission.required_actions or {}).get("deadline_conflicts") or info["deadline_conflicts"],
        "readiness_status": submission.readiness_status,
    }


def _identify_missing_documents(
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
        "assembled_files": submission.assembled_files if submission else {},
        "completed_actions": submission.completed_actions if submission else {},
        "package_manifest_hash": submission.package_manifest_hash if submission else None,
        "missing_documents": missing,
        "proposal_status": proposal.status if proposal else None,
        "submission_method": submission.submission_method if submission else None,
        "destination": submission.submission_destination if submission else None,
        "recipient_email": submission.recipient_email if submission else None,
        "deadline": submission.submission_deadline.isoformat() if submission and submission.submission_deadline else None,
        "deadline_timezone": submission.deadline_timezone if submission else None,
    }
