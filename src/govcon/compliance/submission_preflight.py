"""Final submission-package pre-flight and the ``ready_to_submit`` gate (§15.18).

Every machine-checkable item must be green before READY TO SUBMIT. An item
that cannot be verified is ``unknown`` — never green. The AI pre-flight
prompt and the JEV ``submission_readiness`` bundle are advisory and can only
make the result stricter.

``move_to_ready_to_submit`` is the only path that sets a pursuit to
``ready_to_submit``. Unresolved mandatory/critical blockers refuse the
transition unless an authorized user overrides with an explicit reason; the
override and the blockers it bypassed are written to ``audit_events``.
Submission itself stays a human action (Phase 11).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.ai.structured import StructuredCallError, run_structured_prompt
from govcon.audit import record_audit
from govcon.collaboration.users import require_permission
from govcon.compliance import deterministic as det
from govcon.compliance.deterministic import SubmissionPackage
from govcon.compliance.matrix import (
    active_requirements,
    close_undetected_findings,
    latest_run,
    open_findings,
    record_run,
    upsert_open_finding,
)
from govcon.compliance.records import RESOLVED_STATUSES, ValidatorResult
from govcon.concurrency import apply_versioned_update
from govcon.config import Settings, get_settings
from govcon.models import Opportunity, Pursuit, Requirement, Submission, User

PREFLIGHT_VERSION = "submission_preflight.v1"
GREEN = frozenset({"pass", "not_applicable"})
NON_OVERRIDABLE = frozenset({"deadline"})
TERMINAL_STAGES = frozenset({"submitted", "won", "lost", "cancelled", "no_bid"})


class ReadinessBlocked(RuntimeError):
    def __init__(self, blockers: list[dict[str, Any]]) -> None:
        self.blockers = blockers
        super().__init__(f"{len(blockers)} blocker(s) prevent ready_to_submit: " + "; ".join(b["description"][:120] for b in blockers[:5]))


@dataclass
class Instructions:
    required_files: list[str]
    required_files_confirmed_none: bool
    forms: list[str]
    allowed_file_types: list[str]
    max_file_size_mb: float | None
    required_filenames: list[str]
    recipient_email: str | None
    portal: str | None
    page_limit: int | None
    deadline: datetime | None
    timezone: str | None
    known_amendments: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in self.__dict__.items()}


def requirement_state_hash(requirements: list[Requirement]) -> str:
    basis = sorted((r.id, r.version, r.status, r.blocks_submission) for r in requirements)
    return hashlib.sha256(json.dumps(basis).encode()).hexdigest()


def collect_instructions(opportunity: Opportunity, requirements: list[Requirement], submission: Submission | None, known_amendments: list[str]) -> Instructions:
    kv = [r.key_values or {} for r in requirements]
    forms = sorted({f for k in kv for f in k.get("forms") or []})
    types_sets = [set(k["allowed_file_types"]) for k in kv if k.get("allowed_file_types")]
    sizes = [float(k["max_file_size_mb"]) for k in kv if k.get("max_file_size_mb") is not None]
    pages = [int(k["page_limit"]) for k in kv if k.get("page_limit") is not None]
    recipients = sorted({k["recipient_email"] for k in kv if k.get("recipient_email")})
    portals = sorted({k["submission_portal"] for k in kv if k.get("submission_portal")})
    timezones = sorted({k["deadline_timezone"] for k in kv if k.get("deadline_timezone")})
    raw = submission.required_files if submission is not None else None
    if isinstance(raw, dict):
        required_files = [str(f) for f in raw.get("files", [])]
    elif isinstance(raw, list):
        required_files = [str(f) for f in raw]
    else:
        required_files = []
    confirmed_none = raw is not None and not required_files
    return Instructions(
        required_files=sorted(set(required_files) | set(forms)),
        required_files_confirmed_none=confirmed_none and not forms,
        forms=forms,
        allowed_file_types=sorted(set.intersection(*types_sets)) if types_sets else [],
        max_file_size_mb=min(sizes) if sizes else None,
        required_filenames=sorted({n for k in kv for n in k.get("required_filenames") or []}),
        recipient_email=(submission.recipient_email if submission and submission.recipient_email else (recipients[0] if len(recipients) == 1 else None)),
        portal=(submission.portal_name if submission and submission.portal_name else (portals[0] if len(portals) == 1 else None)),
        page_limit=min(pages) if pages else None,
        deadline=(submission.submission_deadline if submission and submission.submission_deadline else opportunity.response_deadline),
        timezone=(submission.deadline_timezone if submission and submission.deadline_timezone else (timezones[0] if len(timezones) == 1 else None)),
        known_amendments=known_amendments,
    )


def _item(check: str, result: ValidatorResult | None = None, *, status: str | None = None, reason: str = "", evidence: dict | None = None) -> dict[str, Any]:
    if result is not None:
        return {"check": check, "status": result.status, "reason": result.reason, "evidence": result.evidence, "validator": result.validator, "validator_version": result.validator_version}
    return {"check": check, "status": status, "reason": reason, "evidence": evidence or {}, "validator": check, "validator_version": det.VALIDATOR_VERSION}


def preflight_items(
    ins: Instructions,
    package: SubmissionPackage,
    requirements: list[Requirement],
    blocking_findings: list[Any],
    *,
    inventory_complete: bool,
    coverage_ok: bool | None,
    now: datetime,
) -> list[dict[str, Any]]:
    """Pure: the §15.18 checklist."""
    types = {r.requirement_type for r in requirements}
    na = "not_applicable" if inventory_complete else "unknown"
    na_reason = "no such instruction in the matrix" if inventory_complete else "no instruction found, but the source inventory is incomplete"
    items: list[dict[str, Any]] = []

    has_proposal = any(f.role == "proposal" for f in package.files)
    if not has_proposal:
        items.append(_item("proposal_content", status="fail", reason="package has no proposal file"))
    elif coverage_ok is None:
        items.append(_item("proposal_content", status="unknown", reason="no proposal coverage run for this proposal version"))
    else:
        items.append(_item("proposal_content", status="pass" if coverage_ok else "fail", reason="proposal coverage has no blocking gaps" if coverage_ok else "proposal coverage has blocking gaps"))

    if "pricing" in types or any(f.role == "pricing" for f in package.files):
        if not any(f.role == "pricing" for f in package.files):
            items.append(_item("pricing_workbook", status="fail", reason="pricing requirements exist but no pricing workbook is in the package", evidence={"artifact_absent": True}))
        else:
            items.append(_item("pricing_workbook", det.pricing_rows_populated(package)))
    else:
        items.append(_item("pricing_workbook", status=na, reason=na_reason))

    if "signature" in types or any(f.role in {"form", "acknowledgment"} for f in package.files):
        items.append(_item("signed_documents", det.signatures_confirmed(package, ins.forms or None)))
    else:
        items.append(_item("signed_documents", status=na, reason=na_reason))

    for check, field, rtypes in (("representations", "representations_complete", {"representation"}), ("certifications", "certifications_complete", {"certification", "set_aside"})):
        if types & rtypes:
            value = getattr(package, field)
            items.append(_item(check, status={True: "pass", False: "fail", None: "unknown"}[value], reason=f"{field} = {value}"))
        else:
            items.append(_item(check, status=na, reason=na_reason))

    items.append(_item("amendment_acknowledgments", det.amendments_acknowledged(ins.known_amendments, package)))

    if ins.required_files:
        items.append(_item("required_attachments", det.required_files_present(ins.required_files, package)))
    elif ins.required_files_confirmed_none:
        items.append(_item("required_attachments", status="pass", reason="submission record confirms no additional required files"))
    else:
        items.append(_item("required_attachments", status="unknown", reason="required file list is not established; record it on the submission"))

    items.append(_item("filenames", det.filenames_match(ins.required_filenames, package)) if ins.required_filenames else _item("filenames", status=na, reason=na_reason))
    items.append(_item("file_types", det.file_types_allowed(ins.allowed_file_types, package)) if ins.allowed_file_types else _item("file_types", status=na, reason=na_reason))
    items.append(_item("file_sizes", det.file_size_within_limit(ins.max_file_size_mb, package)) if ins.max_file_size_mb is not None else _item("file_sizes", status=na, reason=na_reason))
    items.append(_item("page_limit", det.page_count_within_limit(ins.page_limit, package)) if ins.page_limit is not None else _item("page_limit", status=na, reason=na_reason))

    if ins.recipient_email:
        items.append(_item("recipient", det.recipient_matches(ins.recipient_email, package)))
    if ins.portal:
        items.append(_item("destination", det.portal_matches(ins.portal, package)))
    if not ins.recipient_email and not ins.portal:
        items.append(_item("destination", status="unknown", reason="no recipient email or portal is established by the instructions"))

    deadline = det.deadline_not_passed(ins.deadline, now)
    items.append(_item("deadline", deadline))
    if package.planned_submission_at is not None:
        items.append(_item("submission_timing", det.submission_before_deadline(package, ins.deadline)))
    items.append(_item("timezone", status="pass" if ins.timezone else "unknown", reason=f"deadline timezone {ins.timezone}" if ins.timezone else "deadline timezone not established"))

    submission_reqs = [r for r in requirements if r.requirement_type == "submission"]
    unresolved_submission = [r.id for r in submission_reqs if r.status not in RESOLVED_STATUSES]
    if not submission_reqs:
        items.append(_item("submission_instructions", status="unknown", reason="no submission instructions extracted"))
    else:
        items.append(_item("submission_instructions", status="fail" if unresolved_submission else "pass", reason=f"unresolved submission requirements: {unresolved_submission}" if unresolved_submission else "all submission requirements resolved", evidence={"requirement_ids": unresolved_submission}))

    stale = [r.id for r in requirements if r.status == "stale"]
    items.append(_item("no_stale_conclusions", status="fail" if stale else "pass", reason=f"stale requirements: {stale}" if stale else "no stale requirements", evidence={"requirement_ids": stale}))
    blocking = [r.id for r in requirements if r.blocks_submission]
    items.append(_item("matrix_blockers", status="fail" if blocking else "pass", reason=f"blocking requirements: {blocking}" if blocking else "no blocking requirements", evidence={"requirement_ids": blocking}))
    items.append(_item("open_blocking_findings", status="fail" if blocking_findings else "pass", reason=f"{len(blocking_findings)} open blocking finding(s)" if blocking_findings else "no open blocking findings", evidence={"finding_ids": [f.id for f in blocking_findings]}))
    items.append(_item("source_inventory_complete", status="pass" if inventory_complete else "fail", reason="source inventory complete" if inventory_complete else "source inventory is incomplete"))
    return items


def run_submission_preflight(
    session: Session,
    opportunity_id: int,
    package: SubmissionPackage,
    *,
    submission_id: int | None = None,
    use_ai: bool = False,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    now = now or datetime.now(UTC)
    opportunity = session.get(Opportunity, opportunity_id)
    if opportunity is None:
        raise ValueError(f"opportunity not found: {opportunity_id}")
    submission = session.get(Submission, submission_id) if submission_id else None
    requirements = active_requirements(session, opportunity_id)
    inventory_run = latest_run(session, opportunity_id, "document_inventory")
    inventory_complete = bool(inventory_run and inventory_run.status == "complete")
    documents = (inventory_run.output_json or {}).get("documents", []) if inventory_run else []
    known = sorted({f"{d['amendment_number']:04d}" for d in documents if d.get("document_type") == "amendment" and d.get("amendment_number")})
    coverage_ok: bool | None = None
    coverage = latest_run(session, opportunity_id, "proposal_coverage")
    if coverage and package.proposal_version_id and coverage.output_json.get("proposal_version_id") == package.proposal_version_id:
        coverage_ok = not coverage.output_json.get("blocking_finding_ids")
    ins = collect_instructions(opportunity, requirements, submission, known)
    blocking_findings = [f for f in open_findings(session, opportunity_id, blocking_only=True) if f.detected_by != "submission_preflight"]
    items = preflight_items(ins, package, requirements, blocking_findings, inventory_complete=inventory_complete, coverage_ok=coverage_ok, now=now)
    deterministic_ready = all(i["status"] in GREEN for i in items)

    warnings: list[dict[str, Any]] = []
    ai_summary: dict[str, Any] = {"status": "not_run"}
    ai_issues: list[dict[str, Any]] = []
    if use_ai:
        try:
            ai = run_structured_prompt(
                session,
                opportunity_id=opportunity_id,
                prompt_name="submission_preflight_ai",
                analysis_type="submission_preflight",
                variables={
                    "REQUIREMENTS_JSON": [{"requirement_id": r.id, "text": r.requirement_text, "status": r.status, "severity": r.severity, "blocks_submission": r.blocks_submission} for r in requirements],
                    "SUBMISSION_INSTRUCTIONS_JSON": ins.as_dict(),
                    "EVIDENCE_JSON": {"deterministic_preflight": items, "package": package.manifest(), "amendments": known},
                },
                context_manifest={"requirement_ids": [r.id for r in requirements], "proposal_version_id": package.proposal_version_id},
                settings=settings,
            )
            ai_issues = [i.model_dump() for i in ai.output.issues]
            ai_summary = {"status": "complete", "ai_analysis_id": ai.analysis.id, "ai_status": ai.output.status, "unresolved": ai.output.unresolved}
        except StructuredCallError as exc:
            warnings.append({"code": f"preflight_ai_{exc.reason}", "severity": "medium", "message": exc.detail})
            ai_summary = {"status": "failed", "reason": exc.reason}

    jev = _jev_readiness(session, opportunity_id, requirements, deterministic_ready, settings)
    status = "ready" if deterministic_ready else "not_ready"
    if status == "ready" and (ai_summary.get("ai_status") in {"NOT_READY", "NEEDS_REVIEW"} or ai_issues or jev.get("status") == "not_ready"):
        status = "needs_review"
    run = record_run(
        session,
        opportunity_id=opportunity_id,
        run_type="submission_preflight",
        run_version=PREFLIGHT_VERSION,
        output={},
        status="complete",
        warnings=warnings,
        input_hash=hashlib.sha256(json.dumps(package.manifest(), sort_keys=True, default=str).encode()).hexdigest(),
    )
    kept: set[int] = set()
    for item in items:
        if item["status"] in GREEN:
            continue
        finding = upsert_open_finding(
            session,
            opportunity_id=opportunity_id,
            finding_type=f"preflight_{item['check']}",
            severity="critical" if item["status"] == "fail" else "high",
            description=f"PRE-FLIGHT {item['status'].upper()}: {item['check']} — {item['reason']}",
            detected_by="submission_preflight",
            detector_version=PREFLIGHT_VERSION,
            source_refs=item,
            blocks_submission=True,
            certainty="confirmed" if item["status"] == "fail" else "possible",
            compliance_run_id=run.id,
        )
        kept.add(finding.id)
    for issue in ai_issues:
        finding = upsert_open_finding(
            session,
            opportunity_id=opportunity_id,
            finding_type=f"preflight_ai_{issue['issue_type']}",
            severity=issue["severity"],
            description=f"PRE-FLIGHT AI: {issue['description']}",
            detected_by="submission_preflight_ai",
            detector_version=PREFLIGHT_VERSION,
            source_refs=issue,
            blocks_submission=issue["severity"] in {"critical", "high"},
            certainty="possible",
            compliance_run_id=run.id,
        )
    close_undetected_findings(session, opportunity_id, detected_by="submission_preflight", keep_ids=kept, run_id=run.id)
    run.output_json = {
        "status": status,
        "ready": status == "ready",
        "deterministic_ready": deterministic_ready,
        "items": items,
        "instructions": ins.as_dict(),
        "package": package.manifest(),
        "requirement_state_hash": requirement_state_hash(requirements),
        "ai": ai_summary,
        "jev": jev,
    }
    if submission is not None:
        submission.readiness_status = status
    session.flush()
    return {"run_id": run.id, **run.output_json}


def _jev_readiness(session: Session, opportunity_id: int, requirements: list[Requirement], deterministic_ready: bool, settings: Settings) -> dict[str, Any]:
    from govcon.compliance.metrics import coverage_counts
    from govcon.compliance.validator import compliance_state
    from govcon.decision.engine import build_decision_state, run_decision_bundle

    counts = coverage_counts(requirements, [])
    state = build_decision_state(session, opportunity_id)
    state["compliance"] = compliance_state(requirements, counts)
    state["preflight"] = {"deterministic_ready": deterministic_ready}
    execution = run_decision_bundle(session, opportunity_id=opportunity_id, bundle_name="submission_readiness", state=state, settings=settings)
    return {"decision_run_id": execution.run.id, "provider": execution.provider, **execution.result, "advisory": "JEV cannot move the pursuit; application rules and a human action do."}


def readiness_blockers(session: Session, opportunity_id: int) -> list[dict[str, Any]]:
    requirements = active_requirements(session, opportunity_id)
    blockers: list[dict[str, Any]] = []
    for req in requirements:
        unresolved = req.status not in RESOLVED_STATUSES
        if req.blocks_submission or (unresolved and (req.severity == "critical" or req.mandatory is not False)):
            blockers.append({"kind": "requirement", "id": req.id, "status": req.status, "severity": req.severity, "description": f"requirement {req.id} is {req.status}: {req.requirement_text[:160]}"})
    for finding in open_findings(session, opportunity_id, blocking_only=True):
        kind = "deadline" if finding.finding_type == "preflight_deadline" else "finding"
        blockers.append({"kind": kind, "id": finding.id, "severity": finding.severity, "description": f"finding {finding.id} ({finding.finding_type}): {finding.description[:160]}"})
    preflight = latest_run(session, opportunity_id, "submission_preflight")
    if preflight is None:
        blockers.append({"kind": "preflight", "id": None, "description": "no submission pre-flight has been run"})
    else:
        if not preflight.output_json.get("ready"):
            blockers.append({"kind": "preflight", "id": preflight.id, "description": f"latest pre-flight status is {preflight.output_json.get('status')}"})
        if preflight.output_json.get("requirement_state_hash") != requirement_state_hash(requirements):
            blockers.append({"kind": "preflight", "id": preflight.id, "description": "compliance matrix changed after the latest pre-flight; re-run pre-flight"})
    return blockers


def move_to_ready_to_submit(
    session: Session,
    opportunity_id: int,
    *,
    actor: User,
    override_reason: str | None = None,
) -> Pursuit:
    require_permission(actor, "approve")
    pursuit = session.scalar(select(Pursuit).where(Pursuit.opportunity_id == opportunity_id))
    if pursuit is None:
        raise ValueError(f"no pursuit for opportunity {opportunity_id}")
    if pursuit.stage in TERMINAL_STAGES:
        raise ValueError(f"pursuit is already {pursuit.stage}")
    blockers = readiness_blockers(session, opportunity_id)
    if blockers:
        if not override_reason or len(override_reason.strip()) < 10:
            raise ReadinessBlocked(blockers)
        require_permission(actor, "override_compliance")
        hard = [b for b in blockers if b["kind"] in NON_OVERRIDABLE]
        if hard:
            raise ReadinessBlocked(hard)
        record_audit(
            session,
            action_type="compliance_readiness_override",
            user_id=actor.id,
            opportunity_id=opportunity_id,
            entity_type="pursuits",
            entity_id=pursuit.id,
            old_value={"stage": pursuit.stage, "blockers": blockers},
            new_value={"stage": "ready_to_submit", "reason": override_reason.strip()},
        )
    old_stage = pursuit.stage
    apply_versioned_update(session, pursuit, pursuit.version, {"stage": "ready_to_submit"})
    record_audit(
        session,
        action_type="pursuit_stage_changed",
        user_id=actor.id,
        opportunity_id=opportunity_id,
        entity_type="pursuits",
        entity_id=pursuit.id,
        old_value={"stage": old_stage},
        new_value={"stage": "ready_to_submit", "override": bool(blockers)},
    )
    return pursuit
