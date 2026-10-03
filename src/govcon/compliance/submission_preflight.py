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
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.ai.analysis_types import AnalysisType
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
from govcon.compliance.schemas import SubmissionPreflightAIV1, parse_model
from govcon.concurrency import apply_versioned_update
from govcon.config import Settings, get_settings
from govcon.models import (
    Opportunity,
    Pursuit,
    Requirement,
    ReviewSession,
    Submission,
    User,
)
from govcon.security.classification import DataClassification
from govcon.workflow.source_revision import (
    SOURCE_REVISION_KEY,
    current_source_revision,
    is_stale,
)
from govcon.workflow.transitions import InvalidTransition, require_transition

PREFLIGHT_VERSION = "submission_preflight.v1"
GREEN = frozenset({"pass", "not_applicable"})
NON_OVERRIDABLE = frozenset({"deadline", "package_integrity", "submission_destination_conflict"})
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
    destination_conflicts: dict[str, Any] = field(default_factory=dict)
    # Disagreeing deadlines/time zones, and persisted instruction fields that no
    # longer match the current source. Omitted from ``as_dict`` when empty so a
    # clean pre-flight's saved instructions keep their earlier shape.
    deadline_conflicts: dict[str, Any] = field(default_factory=dict)
    stale_fields: list[str] = field(default_factory=list)
    # File-type rules when any is scoped to one artifact role, and
    # contradictory rules (``allowed_file_types`` holds the global intersection).
    file_type_rules: list[dict[str, Any]] = field(default_factory=list)
    file_type_conflicts: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            k: (v.isoformat() if isinstance(v, datetime) else v)
            for k, v in self.__dict__.items()
            if not (k in _OPTIONAL_INSTRUCTION_KEYS and not v)
        }


_OPTIONAL_INSTRUCTION_KEYS = frozenset({"deadline_conflicts", "stale_fields", "file_type_rules", "file_type_conflicts"})
# A file-type rule is scoped to one artifact role only when its text names
# that document (and only that one) and nothing marks it as applying to
# everything. Anything else is a global rule that every file must satisfy.
_ARTIFACT_SCOPES = (
    ("pricing", re.compile(
        r"\b(?:price|pricing|cost)\s+(?:schedule|sheet|workbook|volume|proposal|quote|list|spreadsheet|template)s?\b|\bbid\s+schedule\b|\bCLINs?\b",
        re.IGNORECASE,
    )),
    ("proposal", re.compile(
        r"\btechnical\s+(?:proposal|volume|quote|quotation|narrative|response|submission)s?\b|\bnarrative\s+(?:volume|response)s?\b",
        re.IGNORECASE,
    )),
)
_APPLIES_TO_ALL = re.compile(r"\b(?:all|each|every|entire|any)\b", re.IGNORECASE)


def file_type_scope(requirement: Requirement) -> str | None:
    """The artifact role a file-type rule is limited to, or None for a rule on every file."""
    text = f"{requirement.requirement_text or ''} {requirement.source_quote or ''}"
    if _APPLIES_TO_ALL.search(text):
        return None
    named = [role for role, pattern in _ARTIFACT_SCOPES if pattern.search(text)]
    if not named and requirement.requirement_type == "pricing":
        named = ["pricing"]
    return named[0] if len(named) == 1 else None


def file_type_rules(requirements: list[Requirement]) -> list[dict[str, Any]]:
    """One rule per requirement that restricts file types: scope, allowed types, source."""
    rules = []
    for req in requirements:
        allowed = (req.key_values or {}).get("allowed_file_types")
        if allowed:
            rules.append({"requirement_id": req.id, "scope": file_type_scope(req), "allowed": sorted({str(t).upper() for t in allowed})})
    return rules


def file_type_conflicts(rules: list[dict[str, Any]]) -> dict[str, Any]:
    """Rules that no single file type can satisfy, keyed by scope (``"all"`` for global)."""
    conflicts: dict[str, Any] = {}
    global_rules = [r for r in rules if r["scope"] is None]
    for scope in sorted({r["scope"] for r in rules if r["scope"]} | ({None} if global_rules else set()), key=str):
        applicable = [r for r in rules if r["scope"] in {scope, None}]
        if len(applicable) > 1 and not set.intersection(*(set(r["allowed"]) for r in applicable)):
            conflicts[scope or "all"] = [{"requirement_id": r["requirement_id"], "allowed": r["allowed"]} for r in applicable]
    return conflicts


def file_types_item(ins: Instructions, package: SubmissionPackage, *, na: str, na_reason: str) -> dict[str, Any]:
    """Each file must satisfy every rule that applies to it: global rules plus rules scoped to its role."""
    if ins.file_type_conflicts:
        scopes = ", ".join(sorted(ins.file_type_conflicts))
        return _item("file_types", status="fail", reason=f"contradictory allowed file types ({scopes}); resolve which instruction controls", evidence={"conflicts": ins.file_type_conflicts})
    if not ins.file_type_rules:
        return _item("file_types", det.file_types_allowed(ins.allowed_file_types, package)) if ins.allowed_file_types else _item("file_types", status=na, reason=na_reason)
    if not package.files:
        return _item("file_types", status="unknown", reason="submission package has not been assembled")
    bad: list[str] = []
    for file in package.files:
        applicable = [set(r["allowed"]) for r in ins.file_type_rules if r["scope"] is None or r["scope"] == file.role]
        if applicable and file.extension not in set.intersection(*applicable):
            bad.append(file.name)
    if bad:
        return _item("file_types", status="fail", reason=f"disallowed file type(s): {', '.join(bad)}", evidence={"rules": ins.file_type_rules, "bad": bad})
    return _item("file_types", status="pass", reason="every file satisfies the file-type rules that apply to it", evidence={"rules": ins.file_type_rules})


def requirement_state_hash(requirements: list[Requirement]) -> str:
    basis = sorted((r.id, r.version, r.status, r.blocks_submission) for r in requirements)
    return hashlib.sha256(json.dumps(basis).encode()).hexdigest()


def collect_instructions(opportunity: Opportunity, requirements: list[Requirement], submission: Submission | None, known_amendments: list[str]) -> Instructions:
    from govcon.submissions.service import (
        _deadline_disagrees,
        _extract_submission_info,
        _file_list,
    )
    info = _extract_submission_info(requirements)
    conflicts = info["destination_conflicts"]
    deadline_conflicts = dict(info["deadline_conflicts"])
    response_deadline = opportunity.response_deadline
    if response_deadline is not None and _deadline_disagrees(response_deadline, info["extracted_deadline_dates"]):
        deadline_conflicts["opportunity_deadline"] = [response_deadline.isoformat(), *info["extracted_deadline_dates"]]
    stale_fields: list[str] = []
    if submission is not None and submission.status not in {"submitted", "confirmed", "withdrawn"}:
        # Recorded instructions are source-derived: one that differs from the
        # current source was superseded and must be regenerated, not trusted.
        current = {
            "submission_method": info["submission_method"],
            "submission_destination": info["submission_destination"],
            "portal_name": info["portal_name"],
            "portal_url": info["portal_url"],
            "recipient_email": info["recipient_email"],
            "deadline_timezone": info["deadline_timezone"],
            "submission_deadline": opportunity.response_deadline,
        }
        stale_fields = [name for name, value in current.items() if getattr(submission, name) is not None and getattr(submission, name) != value]
        recorded_files = set(_file_list(submission.required_files) or [])
        if recorded_files - set(info["required_files"]):
            stale_fields.append("required_files")
    kv = [r.key_values or {} for r in requirements]
    forms = sorted({f for k in kv for f in k.get("forms") or []})
    rules = file_type_rules(requirements)
    global_sets = [set(r["allowed"]) for r in rules if r["scope"] is None]
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
    extracted_files = {str(f) for k in kv for f in k.get("required_files") or []}
    extracted_acks = {str(a) for k in kv for a in k.get("amendment_acknowledgments") or []}
    recorded_acks = (submission.required_actions or {}).get("amendment_acknowledgments", []) if submission else []
    return Instructions(
        required_files=sorted(set(required_files) | set(forms) | extracted_files),
        required_files_confirmed_none=confirmed_none and not forms and not extracted_files,
        forms=forms,
        allowed_file_types=sorted(set.intersection(*global_sets)) if global_sets else [],
        max_file_size_mb=min(sizes) if sizes else None,
        required_filenames=sorted({n for k in kv for n in k.get("required_filenames") or []}),
        recipient_email=(submission.recipient_email if submission and submission.recipient_email else (recipients[0] if len(recipients) == 1 else None)),
        portal=(submission.portal_name if submission and submission.portal_name else (portals[0] if len(portals) == 1 else None)),
        page_limit=min(pages) if pages else None,
        deadline=(submission.submission_deadline if submission and submission.submission_deadline else opportunity.response_deadline),
        timezone=(submission.deadline_timezone if submission and submission.deadline_timezone else (timezones[0] if len(timezones) == 1 else None)),
        known_amendments=sorted(set(known_amendments) | extracted_acks | set(recorded_acks)),
        destination_conflicts=conflicts,
        deadline_conflicts=deadline_conflicts,
        stale_fields=stale_fields,
        file_type_rules=rules if any(r["scope"] for r in rules) else [],
        file_type_conflicts=file_type_conflicts(rules),
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
    artifact_problems: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Pure: the §15.18 checklist.

    ``artifact_problems`` are the package's proposal-binding problems
    (``proposal_artifact_problems``); they fail ``package_integrity``.
    """
    types = {r.requirement_type for r in requirements}
    na = "not_applicable" if inventory_complete else "unknown"
    na_reason = "no such instruction in the matrix" if inventory_complete else "no instruction found, but the source inventory is incomplete"
    items: list[dict[str, Any]] = []
    from govcon.submissions.manifest import verify_package
    problems = verify_package(package) + list(artifact_problems or [])
    items.append(_item("package_integrity", status="fail" if problems else "pass", reason="; ".join(problems) if problems else "every assembled file is retrievable and matches its recorded hash and size"))
    items.append(_item("submission_destination_conflict", status="fail" if ins.destination_conflicts else "pass", reason="conflicting submission destinations or methods" if ins.destination_conflicts else "submission destinations are consistent", evidence=ins.destination_conflicts or {}))

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

    for check, flag, rtypes in (("representations", "representations_complete", {"representation"}), ("certifications", "certifications_complete", {"certification", "set_aside"})):
        if types & rtypes:
            value = getattr(package, flag)
            items.append(_item(check, status={True: "pass", False: "fail", None: "unknown"}[value], reason=f"{flag} = {value}"))
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
    items.append(file_types_item(ins, package, na=na, na_reason=na_reason))
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
    if ins.deadline_conflicts:
        items.append(_item("deadline_conflict", status="fail", reason="the solicitation states conflicting deadlines or deadline time zones; resolve which controls", evidence=ins.deadline_conflicts))
    if ins.stale_fields:
        items.append(_item("instructions_current", status="fail", reason=f"recorded submission instructions no longer match the current solicitation ({', '.join(ins.stale_fields)}); regenerate the submission instructions", evidence={"fields": ins.stale_fields}))
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
    # Freeze a caller-owned mutable package before recording evidence.
    package = SubmissionPackage.from_dict(json.loads(json.dumps(package.manifest(), default=str)))
    now = now or datetime.now(UTC)
    from govcon.workflow.invalidation import lock_one, lock_opportunity
    opportunity = lock_opportunity(session, opportunity_id)
    if opportunity is None:
        raise ValueError(f"opportunity not found: {opportunity_id}")
    submission = lock_one(session, select(Submission).where(Submission.opportunity_id == opportunity_id))
    if submission_id is not None and (submission is None or submission.id != submission_id):
        raise ValueError("submission does not belong to this opportunity")
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
    from govcon.submissions.manifest import proposal_artifact_problems
    artifact_problems = proposal_artifact_problems(session, opportunity_id, package)
    items = preflight_items(ins, package, requirements, blocking_findings, inventory_complete=inventory_complete, coverage_ok=coverage_ok, now=now, artifact_problems=artifact_problems)
    deterministic_ready = all(i["status"] in GREEN for i in items)

    warnings: list[dict[str, Any]] = []
    ai_summary: dict[str, Any] = {"status": "not_run"}
    ai_issues: list[dict[str, Any]] = []
    if use_ai:
        try:
            ai = run_structured_prompt(
                session,
                classification=DataClassification.PROPRIETARY,
                opportunity_id=opportunity_id,
                prompt_name="submission_preflight_ai",
                analysis_type=AnalysisType.SUBMISSION_PREFLIGHT,
                variables={
                    "REQUIREMENTS_JSON": [{"requirement_id": r.id, "text": r.requirement_text, "status": r.status, "severity": r.severity, "blocks_submission": r.blocks_submission} for r in requirements],
                    "SUBMISSION_INSTRUCTIONS_JSON": ins.as_dict(),
                    "EVIDENCE_JSON": {"deterministic_preflight": items, "package": package.manifest(), "amendments": known},
                },
                context_manifest={"requirement_ids": [r.id for r in requirements], "proposal_version_id": package.proposal_version_id},
                settings=settings,
            )
            output = parse_model(ai.output, SubmissionPreflightAIV1)
            analysis = ai.analysis
            ai_issues = [issue.model_dump() for issue in output.issues]
            ai_summary = {
                "status": "complete",
                "ai_analysis_id": analysis.id if analysis is not None else None,
                "ai_status": output.status,
                "unresolved": output.unresolved,
            }
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
        SOURCE_REVISION_KEY: current_source_revision(session, opportunity_id),
        "ai": ai_summary,
        "jev": jev,
    }
    if submission is not None:
        from govcon.submissions.manifest import snapshot_package
        snapshot_package(session, submission, package)
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
        kind = {"preflight_deadline": "deadline", "preflight_package_integrity": "package_integrity", "submission_destination_conflict": "submission_destination_conflict", "preflight_submission_destination_conflict": "submission_destination_conflict"}.get(finding.finding_type, "finding")
        blockers.append({"kind": kind, "id": finding.id, "severity": finding.severity, "description": f"finding {finding.id} ({finding.finding_type}): {finding.description[:160]}"})
    preflight = latest_run(session, opportunity_id, "submission_preflight")
    if preflight is None:
        blockers.append({"kind": "preflight", "id": None, "description": "no submission pre-flight has been run"})
    else:
        if not preflight.output_json.get("ready"):
            blockers.append({"kind": "preflight", "id": preflight.id, "description": f"latest pre-flight status is {preflight.output_json.get('status')}"})
        if preflight.output_json.get("requirement_state_hash") != requirement_state_hash(requirements):
            blockers.append({"kind": "preflight", "id": preflight.id, "description": "compliance matrix changed after the latest pre-flight; re-run pre-flight"})
        if is_stale(preflight.output_json.get(SOURCE_REVISION_KEY), current_source_revision(session, opportunity_id)):
            blockers.append({"kind": "preflight", "id": preflight.id, "description": "the solicitation source changed after the latest pre-flight; re-run pre-flight"})
        from govcon.submissions.manifest import (
            current_package,
            manifest_hash,
            proposal_artifact_problems,
            verify_package,
        )
        submission = session.scalar(select(Submission).where(Submission.opportunity_id == opportunity_id))
        package = current_package(session, submission) if submission else None
        if package is None or manifest_hash(package.manifest()) != manifest_hash(preflight.output_json.get("package") or {}):
            blockers.append({"kind": "package_integrity", "id": preflight.id, "description": "assembled package changed after pre-flight; re-run pre-flight"})
        elif verify_package(package) or proposal_artifact_problems(session, opportunity_id, package):
            problems = verify_package(package) + proposal_artifact_problems(session, opportunity_id, package)
            blockers.append({"kind": "package_integrity", "id": preflight.id, "description": "; ".join(problems)})
        if submission is not None and preflight.output_json.get("instructions"):
            saved = preflight.output_json["instructions"]
            opportunity = session.get(Opportunity, opportunity_id)
            if opportunity is None:
                blockers.append({"kind": "package_integrity", "id": preflight.id, "description": "opportunity is missing; submission instructions cannot be confirmed"})
            else:
                current = collect_instructions(opportunity, requirements, submission, saved.get("known_amendments", [])).as_dict()
                if current != saved:
                    blockers.append({"kind": "package_integrity", "id": preflight.id, "description": "submission instructions changed after pre-flight; re-run pre-flight"})
    return blockers


def move_to_ready_to_submit(
    session: Session,
    opportunity_id: int,
    *,
    actor: User,
    override_reason: str | None = None,
) -> Pursuit:
    require_permission(actor, "approve")
    from govcon.workflow.invalidation import lock_opportunity

    lock_opportunity(session, opportunity_id)  # workflow lock before the pursuit/submission writes
    pursuit = session.scalar(select(Pursuit).where(Pursuit.opportunity_id == opportunity_id))
    if pursuit is None:
        raise ValueError(f"no pursuit for opportunity {opportunity_id}")
    if pursuit.stage in TERMINAL_STAGES:
        raise ValueError(f"pursuit is already {pursuit.stage}")
    try:
        require_transition("pursuit", pursuit.stage, "ready_to_submit")
    except InvalidTransition as exc:
        raise ValueError(f"{exc}; the bid must be approved before submission readiness") from exc
    review = session.scalar(select(ReviewSession).where(ReviewSession.opportunity_id == opportunity_id))
    approved = (
        review.final_approval_status == "approved_to_bid"
        if review is not None
        else pursuit.approved_to_bid_at is not None
    )
    if not approved:
        raise ValueError("the bid is not approved_to_bid; submission readiness requires a human bid approval")
    blockers = readiness_blockers(session, opportunity_id)
    if blockers:
        if not override_reason or len(override_reason.strip()) < 10:
            raise ReadinessBlocked(blockers)
        require_permission(actor, "override_compliance")
        hard = [b for b in blockers if b["kind"] in NON_OVERRIDABLE]
        if hard:
            raise ReadinessBlocked(hard)
        submission = session.scalar(select(Submission).where(Submission.opportunity_id == opportunity_id))
        preflight = latest_run(session, opportunity_id, "submission_preflight")
        record_audit(
            session,
            action_type="compliance_readiness_override",
            user_id=actor.id,
            opportunity_id=opportunity_id,
            entity_type="pursuits",
            entity_id=pursuit.id,
            old_value={"stage": pursuit.stage, "blockers": blockers},
            new_value={"stage": "ready_to_submit", "reason": override_reason.strip(), "package_manifest_sha256": submission.package_manifest_hash if submission else None, "preflight_run_id": preflight.id if preflight else None},
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
