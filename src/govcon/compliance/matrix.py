"""Source-backed compliance matrix: runs, findings, evidence, and the status gate.

``decide_status`` is the only place that turns validation inputs into a
compliance state, and ``apply_decision`` / ``override_requirement`` are the
only writers of ``requirements.status`` after reconciliation. The gate
enforces the Phase 9 invariants:

- ``satisfied`` needs supporting evidence or a deterministic validator pass;
- a deterministic failure cannot become ``satisfied`` except by an explicit,
  audited human override that acknowledges the failure;
- ``unknown`` never collapses into ``missing`` or ``satisfied``;
- critical requirements need two independent validation methods; two AI
  validations count as two only when they ran on different, known
  provider/model pairs;
- proposal evidence counts only for the proposal version being assessed, and
  ambiguous coverage of that version blocks automatic satisfaction;
- stale conclusions are never green;
- a requirement without an established source location is ``needs_review``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from govcon.audit import record_audit
from govcon.collaboration.users import require_permission
from govcon.compliance.records import ARTIFACT_TYPES, RESOLVED_STATUSES
from govcon.concurrency import apply_versioned_update
from govcon.models import (
    ComplianceFinding,
    ComplianceRun,
    Requirement,
    RequirementEvidence,
    StoredFile,
    User,
)

EVIDENCE_TYPES = frozenset(
    {
        "company_registration", "supplier_quote", "supplier_spec_sheet", "company_record",
        "proposal_section", "signed_form", "pricing_workbook", "validator_output",
        "human_verification", "submission_package",
    }
)
VERIFICATION_METHODS = frozenset(
    {
        "deterministic", "ai_extraction", "ai_validation", "human", "supplier_document",
        "company_record", "proposal_scan", "submission_preflight",
    }
)
VERIFICATION_STATUSES = frozenset({"unverified", "verified", "conflicting", "insufficient"})
BLOCKING_FLAGS = frozenset({"ab_disagreement", "conflict_ambiguous", "citation_unverified", "mandatory_uncertain"})
OVERRIDE_STATUSES = frozenset({"satisfied", "missing", "unknown", "needs_review", "not_applicable"})


class ComplianceInvariantError(RuntimeError):
    """A status change would violate a compliance invariant."""


# ── runs and findings ──


def record_run(
    session: Session,
    *,
    opportunity_id: int,
    run_type: str,
    run_version: str,
    output: dict[str, Any],
    status: str = "complete",
    warnings: list[dict[str, Any]] | None = None,
    source_snapshot_ids: list[int] | None = None,
    input_hash: str | None = None,
    counts: dict[str, int] | None = None,
) -> ComplianceRun:
    run = ComplianceRun(
        opportunity_id=opportunity_id,
        run_type=run_type,
        run_version=run_version,
        source_snapshot_ids=source_snapshot_ids or None,
        input_hash=input_hash,
        output_json=output,
        status=status,
        warnings=warnings or None,
        **(counts or {}),
    )
    session.add(run)
    session.flush()
    return run


def latest_run(session: Session, opportunity_id: int, run_type: str) -> ComplianceRun | None:
    return session.scalar(
        select(ComplianceRun)
        .where(ComplianceRun.opportunity_id == opportunity_id, ComplianceRun.run_type == run_type)
        .order_by(desc(ComplianceRun.created_at), desc(ComplianceRun.id))
        .limit(1)
    )


def upsert_open_finding(
    session: Session,
    *,
    opportunity_id: int,
    finding_type: str,
    severity: str,
    description: str,
    detected_by: str,
    detector_version: str,
    requirement_id: int | None = None,
    source_refs: dict[str, Any] | list | None = None,
    blocks_submission: bool = False,
    certainty: str | None = "confirmed",
    compliance_run_id: int | None = None,
) -> ComplianceFinding:
    """Persist a finding once; a repeat detection refreshes the open row.

    A finding a human resolved stays resolved while the detected facts (and
    therefore the description) are unchanged.
    """
    same = (
        select(ComplianceFinding)
        .where(
            ComplianceFinding.opportunity_id == opportunity_id,
            ComplianceFinding.finding_type == finding_type,
            ComplianceFinding.description == description,
            (ComplianceFinding.requirement_id == requirement_id)
            if requirement_id is not None
            else ComplianceFinding.requirement_id.is_(None),
        )
        .order_by(desc(ComplianceFinding.id))
    )
    existing = session.scalar(same.where(ComplianceFinding.status == "open"))
    if existing is None:
        human_resolved = session.scalar(
            same.where(ComplianceFinding.status == "resolved", ~ComplianceFinding.resolution_notes.like("no longer detected%"))
        )
        if human_resolved is not None:
            return human_resolved
    refs = source_refs if isinstance(source_refs, dict) or source_refs is None else {"items": source_refs}
    if existing is not None:
        existing.severity = severity
        existing.blocks_submission = blocks_submission
        existing.source_refs = refs
        existing.detector_version = detector_version
        existing.compliance_run_id = compliance_run_id or existing.compliance_run_id
        session.flush()
        return existing
    finding = ComplianceFinding(
        opportunity_id=opportunity_id,
        requirement_id=requirement_id,
        finding_type=finding_type,
        severity=severity,
        description=description,
        source_refs=refs,
        status="open",
        detected_by=detected_by,
        detector_version=detector_version,
        blocks_submission=blocks_submission,
        certainty=certainty,
        compliance_run_id=compliance_run_id,
    )
    session.add(finding)
    session.flush()
    return finding


def close_undetected_findings(
    session: Session,
    opportunity_id: int,
    *,
    detected_by: str,
    keep_ids: set[int],
    run_id: int,
    finding_types: set[str] | None = None,
) -> list[int]:
    """Resolve open deterministic findings that the latest run of that detector no longer reproduces."""
    closed: list[int] = []
    for finding in session.scalars(
        select(ComplianceFinding).where(
            ComplianceFinding.opportunity_id == opportunity_id,
            ComplianceFinding.detected_by == detected_by,
            ComplianceFinding.status == "open",
        )
    ).all():
        if finding.id in keep_ids or (finding_types is not None and finding.finding_type not in finding_types):
            continue
        finding.status = "resolved"
        finding.resolved_at = datetime.now(UTC)
        finding.resolution_notes = f"no longer detected by {detected_by} (compliance run {run_id})"
        closed.append(finding.id)
    session.flush()
    return closed


def open_findings(session: Session, opportunity_id: int, *, blocking_only: bool = False) -> list[ComplianceFinding]:
    query = select(ComplianceFinding).where(
        ComplianceFinding.opportunity_id == opportunity_id, ComplianceFinding.status == "open"
    )
    if blocking_only:
        query = query.where(ComplianceFinding.blocks_submission.is_(True))
    return list(session.scalars(query.order_by(ComplianceFinding.id)).all())


def resolve_finding(session: Session, finding_id: int, *, actor: User, notes: str) -> ComplianceFinding:
    require_permission(actor, "review")
    if not notes or not notes.strip():
        raise ValueError("resolving a compliance finding requires resolution notes")
    finding = session.get(ComplianceFinding, finding_id)
    if finding is None:
        raise ValueError(f"finding not found: {finding_id}")
    if finding.blocks_submission:
        require_permission(actor, "override_compliance")
    old = {"status": finding.status}
    finding.status = "resolved"
    finding.resolved_at = datetime.now(UTC)
    finding.resolution_notes = notes.strip()
    record_audit(
        session,
        action_type="compliance_finding_resolved",
        user_id=actor.id,
        opportunity_id=finding.opportunity_id,
        entity_type="compliance_findings",
        entity_id=finding.id,
        old_value=old,
        new_value={"status": "resolved", "notes": finding.resolution_notes, "blocks_submission": finding.blocks_submission},
    )
    session.flush()
    return finding


# ── evidence ──


def add_evidence(
    session: Session,
    *,
    requirement_id: int,
    evidence_type: str,
    verification_method: str,
    verification_status: str = "unverified",
    description: str | None = None,
    source_file_id: int | None = None,
    proposal_version_id: int | None = None,
    source_page: int | None = None,
    source_section: str | None = None,
    source_quote: str | None = None,
    evidence_value: dict[str, Any] | None = None,
) -> RequirementEvidence:
    if evidence_type not in EVIDENCE_TYPES:
        raise ValueError(f"unknown evidence_type {evidence_type!r}")
    if verification_method not in VERIFICATION_METHODS:
        raise ValueError(f"unknown verification_method {verification_method!r}")
    if verification_status not in VERIFICATION_STATUSES:
        raise ValueError(f"unknown verification_status {verification_status!r}")
    row = RequirementEvidence(
        requirement_id=requirement_id,
        evidence_type=evidence_type,
        source_file_id=source_file_id,
        proposal_version_id=proposal_version_id,
        description=description,
        source_page=source_page,
        source_section=source_section,
        source_quote=source_quote,
        evidence_value=evidence_value,
        verification_method=verification_method,
        verification_status=verification_status,
    )
    session.add(row)
    session.flush()
    return row


def record_human_verification(
    session: Session,
    *,
    requirement_id: int,
    actor: User,
    description: str,
    verified: bool = True,
    source_file_id: int | None = None,
    source_page: int | None = None,
    source_quote: str | None = None,
) -> RequirementEvidence:
    """A reviewer confirms (or rejects) evidence; counts as the ``human`` validation method."""
    require_permission(actor, "review")
    if not description or not description.strip():
        raise ValueError("human verification requires a description of what was verified")
    row = add_evidence(
        session,
        requirement_id=requirement_id,
        evidence_type="human_verification",
        verification_method="human",
        verification_status="verified" if verified else "insufficient",
        description=description.strip(),
        source_file_id=source_file_id,
        source_page=source_page,
        source_quote=source_quote,
        evidence_value={"user_id": actor.id},
    )
    requirement = session.get(Requirement, requirement_id)
    record_audit(
        session,
        action_type="compliance_human_verification",
        user_id=actor.id,
        opportunity_id=requirement.opportunity_id if requirement else None,
        entity_type="requirement_evidence",
        entity_id=row.id,
        new_value={"requirement_id": requirement_id, "verified": verified, "description": row.description},
    )
    return row


def evidence_for(session: Session, requirement_ids: list[int]) -> dict[int, list[RequirementEvidence]]:
    if not requirement_ids:
        return {}
    rows = session.scalars(
        select(RequirementEvidence)
        .where(RequirementEvidence.requirement_id.in_(requirement_ids))
        .order_by(RequirementEvidence.id)
    ).all()
    grouped: dict[int, list[RequirementEvidence]] = {rid: [] for rid in requirement_ids}
    for row in rows:
        grouped.setdefault(row.requirement_id, []).append(row)
    return grouped


# ── status gate ──


@dataclass
class ValidationInputs:
    """Everything the gate needs to decide one requirement's state."""

    requirement_type: str | None
    mandatory: bool | None
    severity: str | None
    has_source_location: bool
    current_status: str
    stale: bool
    flags: list[str] = field(default_factory=list)
    deterministic: list[dict[str, Any]] = field(default_factory=list)
    fresh_verified_methods: set[str] = field(default_factory=set)
    verified_evidence_ids: set[int] = field(default_factory=set)
    ai_primary: dict[str, Any] | None = None
    ai_secondary: dict[str, Any] | None = None
    override: dict[str, Any] | None = None
    high_confidence_threshold: float | None = None
    low_confidence_threshold: float | None = None
    jev_human_interpretation: bool = False


@dataclass
class StatusDecision:
    status: str
    reason: str
    blocks_submission: bool
    methods: list[str]
    deterministic_fail: bool
    blocked_ai_claims: list[str] = field(default_factory=list)


def validator_identity(provider: Any, model: Any) -> tuple[str, str] | None:
    """Normalised ``(provider, model)`` of an AI validator; None when unknown."""
    if not provider or not model:
        return None
    return str(provider).strip().lower(), str(model).strip().lower()


def independent_validators(first: dict[str, Any] | None, second: dict[str, Any] | None) -> bool:
    """True only when both AI results carry a known, different provider/model.

    Repeated calls to one model share its blind spots, so they count as one
    validation method; unknown provenance cannot establish independence.
    """
    a = validator_identity((first or {}).get("provider"), (first or {}).get("model"))
    b = validator_identity((second or {}).get("provider"), (second or {}).get("model"))
    return a is not None and b is not None and a != b


def _ai_supported(ai: dict[str, Any] | None, verified_ids: set[int], threshold: float | None, *, require_threshold: bool) -> bool:
    if not ai or ai.get("status") != "SATISFIED":
        return False
    cited = {int(i) for i in ai.get("evidence_ids") or [] if i is not None}
    if not cited or not cited <= verified_ids:
        return False
    if require_threshold:
        confidence = ai.get("confidence")
        return threshold is not None and confidence is not None and float(confidence) >= threshold
    return True


def decide_status(inputs: ValidationInputs) -> StatusDecision:
    """Pure: map validation inputs to one explicit compliance state."""
    potentially_mandatory = inputs.mandatory is not False
    critical = inputs.severity == "critical"
    blocked: list[str] = []

    def done(status: str, reason: str, methods: list[str] | None = None, det_fail: bool = False) -> StatusDecision:
        blocks = status not in RESOLVED_STATUSES and (potentially_mandatory or critical)
        return StatusDecision(status, reason, blocks, sorted(methods or []), det_fail, blocked)

    if inputs.current_status == "superseded":
        return done("superseded", "superseded by a later controlling requirement")

    failures = [d for d in inputs.deterministic if d.get("status") == "fail"]
    unknowns = [d for d in inputs.deterministic if d.get("status") == "unknown"]
    passes = [d for d in inputs.deterministic if d.get("status") == "pass"]
    for ai in (inputs.ai_primary, inputs.ai_secondary):
        if ai and ai.get("status") == "SATISFIED" and failures:
            blocked.append(f"AI proposed SATISFIED but deterministic validator(s) failed: {', '.join(d['validator'] for d in failures)}")

    if inputs.override and not inputs.stale:
        status = inputs.override["status"]
        return done(status, f"human override by user {inputs.override.get('user_id')}: {inputs.override.get('reason')}", ["human_override"], bool(failures))

    if failures:
        artifact = (inputs.requirement_type in ARTIFACT_TYPES) or any(d.get("evidence", {}).get("artifact_absent") for d in failures)
        reason = "; ".join(f"{d['validator']}: {d['reason']}" for d in failures)
        return done("missing" if artifact else "needs_review", f"deterministic failure — {reason}", det_fail=True)

    if inputs.stale:
        return done("stale", "source changed by amendment; conclusion requires revalidation")

    if not inputs.has_source_location and "human" not in inputs.fresh_verified_methods:
        return done("needs_review", "source location could not be established")

    blocking_flags = sorted(set(inputs.flags) & BLOCKING_FLAGS)
    if blocking_flags and "human" not in inputs.fresh_verified_methods:
        return done("needs_review", f"reconciliation flags require review: {', '.join(blocking_flags)}")

    if inputs.jev_human_interpretation and "human" not in inputs.fresh_verified_methods:
        return done("needs_review", "decision layer requires human interpretation")

    coverage_open = [d for d in unknowns if d.get("validator") == "proposal_coverage"]
    if coverage_open and "human" not in inputs.fresh_verified_methods:
        # The current proposal version does not clearly address the requirement;
        # evidence from elsewhere must not satisfy it automatically.
        return done("needs_review", "; ".join(f"proposal coverage: {d.get('reason')}" for d in coverage_open))

    methods: set[str] = set()
    if passes and not unknowns:
        methods.add("deterministic")
    methods |= inputs.fresh_verified_methods & {"human", "proposal_scan"}
    same_ai_validator = False
    primary_supported = _ai_supported(inputs.ai_primary, inputs.verified_evidence_ids, None, require_threshold=False)
    if primary_supported:
        methods.add("ai_validation")
    if _ai_supported(inputs.ai_secondary, inputs.verified_evidence_ids, None, require_threshold=False):
        if primary_supported and not independent_validators(inputs.ai_primary, inputs.ai_secondary):
            # The same (or an unidentified) model asked twice is one method.
            same_ai_validator = True
        else:
            methods.add("ai_validation_secondary")

    for ai in (inputs.ai_primary, inputs.ai_secondary):
        if ai and ai.get("status") == "SATISFIED" and "ai_validation" not in methods and "ai_validation_secondary" not in methods:
            blocked.append("AI proposed SATISFIED without citing verified evidence")

    low = inputs.low_confidence_threshold
    ai_conf = (inputs.ai_primary or {}).get("confidence")
    if methods and low is not None and ai_conf is not None and float(ai_conf) < low and methods <= {"ai_validation", "ai_validation_secondary"}:
        return done("needs_review", "AI validation confidence below configured low threshold", sorted(methods))

    if methods:
        if critical and len(methods) < 2:
            return done(
                "needs_review",
                f"critical requirement requires redundant validation; have {', '.join(sorted(methods))}"
                + ("; the secondary AI validation is not independent of the primary (same or unknown provider/model)" if same_ai_validator else ""),
                sorted(methods),
            )
        if methods == {"ai_validation"}:
            if _ai_supported(inputs.ai_primary, inputs.verified_evidence_ids, inputs.high_confidence_threshold, require_threshold=True) and not unknowns:
                return done("satisfied", "AI validation above configured auto-accept threshold with verified evidence", ["ai_validation"])
            return done("needs_review", "AI-only validation requires a second validation or calibrated auto-accept threshold", ["ai_validation"])
        return done("satisfied", f"validated by {', '.join(sorted(methods))}", sorted(methods))

    ai_status = (inputs.ai_primary or {}).get("status")
    if ai_status == "MISSING":
        return done("missing", (inputs.ai_primary or {}).get("reason") or "AI validator found the required artifact/action absent")
    if ai_status == "NOT_APPLICABLE":
        return done("needs_review", "AI proposed NOT_APPLICABLE; only a human may mark a potentially mandatory requirement not applicable")
    if ai_status in {"NEEDS_REVIEW", "STALE"} or blocked:
        return done("needs_review", (inputs.ai_primary or {}).get("reason") or "; ".join(blocked) or "AI validator requested review")
    if unknowns:
        return done("unknown", "; ".join(f"{d['validator']}: {d['reason']}" for d in unknowns))
    return done("unknown", "no evidence establishes whether this requirement is satisfied")


def assessed_proposal_version_id(requirement: Requirement) -> int | None:
    """The proposal version the latest coverage check assessed for this requirement."""
    coverage = (requirement.validation or {}).get("proposal_coverage") or {}
    value = coverage.get("proposal_version_id")
    return int(value) if value is not None else None


def fresh_evidence(
    requirement: Requirement,
    evidence: list[RequirementEvidence],
    *,
    proposal_version_id: int | None = None,
) -> list[RequirementEvidence]:
    """Evidence that still counts for ``requirement``.

    Only evidence recorded after the latest amendment change counts. Evidence
    tied to a proposal version counts only for the version currently assessed
    (``proposal_version_id``, default: the latest coverage check's version): a
    superseded draft's content says nothing about the current one.
    """
    cutoff = requirement.amendment_changed_at
    target = proposal_version_id if proposal_version_id is not None else assessed_proposal_version_id(requirement)
    return [
        e for e in evidence
        if (cutoff is None or (e.created_at and e.created_at >= cutoff))
        and (e.proposal_version_id is None or target is None or e.proposal_version_id == target)
    ]


def inputs_for(
    requirement: Requirement,
    evidence: list[RequirementEvidence],
    *,
    ai_primary: dict[str, Any] | None = None,
    ai_secondary: dict[str, Any] | None = None,
    settings=None,
    jev_human_interpretation: bool = False,
    proposal_version_id: int | None = None,
) -> ValidationInputs:
    validation = requirement.validation or {}
    reconciliation = requirement.reconciliation or {}
    fresh = [
        e for e in fresh_evidence(requirement, evidence, proposal_version_id=proposal_version_id)
        if e.verification_status == "verified"
    ]
    override = validation.get("override")
    if override and requirement.amendment_changed_at and override.get("at"):
        if datetime.fromisoformat(override["at"]) < requirement.amendment_changed_at:
            override = None
    deterministic = list(validation.get("deterministic") or []) + list(validation.get("proposal_coverage_checks") or [])
    revalidated = bool(fresh) or any(d.get("status") in {"pass", "fail"} for d in deterministic)
    return ValidationInputs(
        requirement_type=requirement.requirement_type,
        mandatory=requirement.mandatory,
        severity=requirement.severity,
        has_source_location=requirement.source_file_id is not None and bool(requirement.source_quote),
        current_status=requirement.status,
        stale=bool(requirement.stale_due_to_amendment) and not revalidated,
        flags=list(reconciliation.get("flags") or []),
        deterministic=deterministic,
        fresh_verified_methods={e.verification_method for e in fresh if e.verification_method},
        verified_evidence_ids={e.id for e in fresh},
        ai_primary=ai_primary,
        ai_secondary=ai_secondary,
        override=override,
        high_confidence_threshold=getattr(settings, "compliance_high_confidence_threshold", None),
        low_confidence_threshold=getattr(settings, "compliance_low_confidence_threshold", None),
        jev_human_interpretation=jev_human_interpretation,
    )


def apply_decision(requirement: Requirement, decision: StatusDecision, *, run_id: int | None = None) -> bool:
    """Write the gate's decision, versioning any status or blocker change."""
    if decision.status == "satisfied" and not decision.methods:
        raise ComplianceInvariantError("satisfied requires evidence or validator output")
    changed = requirement.status != decision.status
    old_blocks = requirement.blocks_submission
    validation = dict(requirement.validation or {})
    validation.update(
        {
            "methods": decision.methods,
            "deterministic_fail": decision.deterministic_fail,
            "last_validated_at": datetime.now(UTC).isoformat(),
            "last_run_id": run_id,
        }
    )
    if decision.blocked_ai_claims:
        validation["blocked_ai_claims"] = decision.blocked_ai_claims
    requirement.validation = validation
    requirement.status = decision.status
    requirement.status_reason = decision.reason
    if decision.status != "stale":
        requirement.stale_due_to_amendment = False
    if decision.status in RESOLVED_STATUSES:
        validation.pop("jev_blocks_submission", None)
        requirement.validation = validation
    requirement.blocks_submission = decision.blocks_submission or bool(validation.get("jev_blocks_submission"))
    changed = changed or old_blocks != requirement.blocks_submission
    if changed:
        requirement.version = (requirement.version or 1) + 1
    return changed


def override_requirement(
    session: Session,
    *,
    requirement_id: int,
    status: str,
    actor: User,
    reason: str,
    expected_version: int,
    acknowledge_deterministic_failure: bool = False,
) -> Requirement:
    """Authorized human override: explicit reason, optimistic version, audit history."""
    require_permission(actor, "override_compliance")
    if status not in OVERRIDE_STATUSES:
        raise ValueError(f"override status must be one of {sorted(OVERRIDE_STATUSES)}")
    if not reason or len(reason.strip()) < 10:
        raise ValueError("override requires an explicit reason (at least 10 characters)")
    requirement = session.get(Requirement, requirement_id)
    if requirement is None:
        raise ValueError(f"requirement not found: {requirement_id}")
    validation = dict(requirement.validation or {})
    deterministic_failures = [d for d in validation.get("deterministic") or [] if d.get("status") == "fail"]
    if status == "satisfied" and deterministic_failures and not acknowledge_deterministic_failure:
        raise ComplianceInvariantError(
            "deterministic validator failure must be explicitly acknowledged to override: "
            + ", ".join(d["validator"] for d in deterministic_failures)
        )
    now = datetime.now(UTC)
    evidence = None
    if status == "satisfied":
        evidence = add_evidence(
            session,
            requirement_id=requirement.id,
            evidence_type="human_verification",
            verification_method="human",
            verification_status="verified",
            description=f"Authorized override: {reason.strip()}",
            evidence_value={"user_id": actor.id, "override": True},
        )
    validation["override"] = {
        "status": status,
        "reason": reason.strip(),
        "user_id": actor.id,
        "at": now.isoformat(),
        "acknowledged_deterministic_failures": [d["validator"] for d in deterministic_failures] if acknowledge_deterministic_failure else [],
        "evidence_id": evidence.id if evidence else None,
    }
    old = {
        "status": requirement.status,
        "status_reason": requirement.status_reason,
        "blocks_submission": requirement.blocks_submission,
        "version": requirement.version,
    }
    blocks = status not in RESOLVED_STATUSES and (requirement.mandatory is not False or requirement.severity == "critical")
    apply_versioned_update(
        session,
        requirement,
        expected_version,
        {
            "status": status,
            "status_reason": f"human override: {reason.strip()}",
            "verified_by_human": True,
            "blocks_submission": blocks,
            "validation": validation,
            "stale_due_to_amendment": False,
        },
    )
    record_audit(
        session,
        action_type="compliance_requirement_override",
        user_id=actor.id,
        opportunity_id=requirement.opportunity_id,
        entity_type="requirements",
        entity_id=requirement.id,
        old_value=old,
        new_value={
            "status": status,
            "reason": reason.strip(),
            "blocks_submission": blocks,
            "deterministic_failures_overridden": validation["override"]["acknowledged_deterministic_failures"],
        },
    )
    return requirement


# ── matrix view ──


MATRIX_FILTERS = (
    "critical_only", "missing", "unknown", "needs_review", "stale", "submission_blockers",
    "recently_changed_by_amendment", "not_independently_confirmed",
)


def active_requirements(session: Session, opportunity_id: int, *, include_superseded: bool = False) -> list[Requirement]:
    query = select(Requirement).where(Requirement.opportunity_id == opportunity_id)
    if not include_superseded:
        query = query.where(Requirement.status != "superseded")
    return list(session.scalars(query.order_by(Requirement.id)).all())


def compliance_matrix(session: Session, opportunity_id: int, *, filters: list[str] | tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Rows carrying every §15.21 column; filters combine with AND."""
    unknown_filters = set(filters) - set(MATRIX_FILTERS)
    if unknown_filters:
        raise ValueError(f"unknown matrix filter(s): {', '.join(sorted(unknown_filters))}")
    requirements = active_requirements(session, opportunity_id, include_superseded=True)
    evidence = evidence_for(session, [r.id for r in requirements])
    files = {
        f.id: f
        for f in session.scalars(select(StoredFile).where(StoredFile.opportunity_id == opportunity_id)).all()
    }
    rows: list[dict[str, Any]] = []
    for req in requirements:
        row = _matrix_row(req, evidence.get(req.id, []), files.get(req.source_file_id) if req.source_file_id is not None else None)
        if _row_matches(row, filters):
            rows.append(row)
    return rows


def _matrix_row(req: Requirement, evidence: list[RequirementEvidence], source_file: StoredFile | None) -> dict[str, Any]:
    reconciliation = req.reconciliation or {}
    return {
        "requirement_id": req.id,
        "requirement": req.requirement_text,
        "requirement_type": req.requirement_type,
        "mandatory": req.mandatory,
        "severity": req.severity,
        "source": {
            "file_id": req.source_file_id,
            "filename": source_file.filename if source_file else None,
            "url": source_file.url if source_file else None,
            "local_path": source_file.local_path if source_file else None,
            "page": req.source_page,
            "section": req.source_section,
            "quote": req.source_quote,
            "snapshot_id": req.source_snapshot_id,
            "all_refs": req.source_refs or [],
        },
        "evidence": [
            {
                "evidence_id": e.id,
                "evidence_type": e.evidence_type,
                "verification_method": e.verification_method,
                "verification_status": e.verification_status,
                "description": e.description,
                "section": e.source_section,
                "page": e.source_page,
            }
            for e in evidence
        ],
        "validator_output": (req.validation or {}).get("deterministic", []),
        "validation_methods": (req.validation or {}).get("methods", []),
        "clause": (req.validation or {}).get("clause"),
        "status": req.status,
        "status_reason": req.status_reason,
        "confidence": float(req.extraction_confidence) if req.extraction_confidence is not None else None,
        "independently_confirmed": req.independently_confirmed,
        "found_by": reconciliation.get("found_by", []),
        "flags": reconciliation.get("flags", []),
        "amendment_freshness": {
            "stale": req.stale_due_to_amendment,
            "changed_by_amendment_at": req.amendment_changed_at.isoformat() if req.amendment_changed_at else None,
            "superseded_by_requirement_id": req.superseded_by_requirement_id,
        },
        "blocking": req.blocks_submission,
        "version": req.version,
    }


def _row_matches(row: dict[str, Any], filters) -> bool:
    checks = {
        "critical_only": row["severity"] == "critical",
        "missing": row["status"] == "missing",
        "unknown": row["status"] == "unknown",
        "needs_review": row["status"] == "needs_review",
        "stale": row["status"] == "stale" or row["amendment_freshness"]["stale"],
        "submission_blockers": bool(row["blocking"]),
        "recently_changed_by_amendment": row["amendment_freshness"]["changed_by_amendment_at"] is not None,
        "not_independently_confirmed": not row["independently_confirmed"],
    }
    return all(checks[name] for name in filters)
