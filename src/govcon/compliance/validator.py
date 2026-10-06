"""Compliance validation orchestration (§15.3 layer 3, §15.12, §15.16).

Validation combines deterministic validator results, recorded evidence, and
optional AI validators, and routes every requirement through
``matrix.decide_status``. AI output is a proposal: it cannot set a status
directly, cannot override a deterministic failure, and cannot claim
SATISFIED without citing verified evidence rows.

Critical requirements get redundant validation regardless of confidence: a
second AI validation runs on ``AI_SECONDARY_REVIEW_PROVIDER`` (or
``AI_REVIEW_PROVIDER``) when configured, and the gate requires two
independent methods before a critical requirement is satisfied. Each AI result
carries the provider and model that produced it; a secondary validation on the
same provider/model counts as the same method as the primary.

JEV routing runs the Phase 8 ``compliance_and_amendment`` bundle on the
structured matrix state. JEV can add blockers or route to human review; it
cannot clear a blocker or mark anything satisfied.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from govcon.ai.analysis_types import AnalysisType
from govcon.ai.structured import (
    StructuredCallError,
    checked_output,
    run_structured_prompt,
)
from govcon.compliance.extractor import independence_warnings
from govcon.compliance.matrix import (
    active_requirements,
    apply_decision,
    decide_status,
    evidence_for,
    fresh_evidence,
    inputs_for,
    record_run,
    upsert_open_finding,
)
from govcon.compliance.records import RESOLVED_STATUSES
from govcon.compliance.schemas import ComplianceValidationV1
from govcon.config import Settings, get_settings
from govcon.models import Requirement, RequirementEvidence
from govcon.security.classification import DataClassification

VALIDATION_VERSION = "compliance_validation.v1"
ROUTING_VERSION = "jev_routing.v1"


def _requirement_payload(req: Requirement) -> dict[str, Any]:
    validation = req.validation or {}
    return {
        "requirement_id": req.id,
        "requirement_text": req.requirement_text,
        "requirement_type": req.requirement_type,
        "mandatory": req.mandatory,
        "severity": req.severity,
        "source": {"file_id": req.source_file_id, "page": req.source_page, "section": req.source_section, "quote": req.source_quote},
        "deterministic_results": validation.get("deterministic", []),
        "clause_guidance": validation.get("clause"),
        "stale": req.stale_due_to_amendment,
    }


def _evidence_payload(rows: list[RequirementEvidence]) -> list[dict[str, Any]]:
    return [
        {
            "evidence_id": e.id,
            "requirement_id": e.requirement_id,
            "evidence_type": e.evidence_type,
            "verification_method": e.verification_method,
            "verification_status": e.verification_status,
            "description": e.description,
            "source_file_id": e.source_file_id,
            "proposal_version_id": e.proposal_version_id,
            "page": e.source_page,
            "section": e.source_section,
            "quote": e.source_quote,
            "value": e.evidence_value,
        }
        for e in rows
    ]


def _ai_validate(
    session: Session,
    opportunity_id: int,
    requirements: list[Requirement],
    evidence: dict[int, list[RequirementEvidence]],
    *,
    settings: Settings,
    provider_name: str | None,
    label: str,
    warnings: list[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    if not requirements:
        return {}
    by_id = {r.id: r for r in requirements}
    rows = [e for r in requirements for e in fresh_evidence(r, evidence.get(r.id, []))]
    try:
        result = run_structured_prompt(
            session,
            classification=DataClassification.PROPRIETARY,
            opportunity_id=opportunity_id,
            prompt_name="compliance_validator",
            analysis_type=AnalysisType.COMPLIANCE_REVIEW,
            variables={"REQUIREMENTS_JSON": [_requirement_payload(r) for r in requirements], "EVIDENCE_JSON": _evidence_payload(rows)},
            context_manifest={"validator": label, "requirement_ids": sorted(by_id), "evidence_ids": [e.id for e in rows]},
            settings=settings,
            provider_name=provider_name,
        )
    except StructuredCallError as exc:
        warnings.append({"code": f"{label}_{exc.reason}", "severity": "medium", "message": f"{label} unavailable: {exc.detail}"})
        return {}
    analysis = result.analysis
    out: dict[int, dict[str, Any]] = {}
    for item in checked_output(result.output, ComplianceValidationV1).validations:
        if item.requirement_id not in by_id:
            warnings.append({"code": f"{label}_unknown_requirement", "severity": "low", "message": f"ignored validation for unknown requirement {item.requirement_id}"})
            continue
        out[item.requirement_id] = {
            "status": item.status,
            "reason": item.reason,
            "confidence": item.confidence,
            "evidence_ids": [ref.evidence_id for ref in item.evidence_refs if ref.evidence_id is not None],
            "analysis_id": analysis.id if analysis is not None else None,
            "provider": analysis.provider if analysis is not None else None,
            "model": analysis.model if analysis is not None else None,
        }
    return out


def run_validation(
    session: Session,
    opportunity_id: int,
    *,
    use_ai: bool = True,
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    requirements = active_requirements(session, opportunity_id)
    evidence = evidence_for(session, [r.id for r in requirements])
    warnings: list[dict[str, Any]] = []
    primary: dict[int, dict[str, Any]] = {}
    secondary: dict[int, dict[str, Any]] = {}
    if use_ai:
        candidates = [
            r for r in requirements
            if any(e.verification_status == "verified" for e in fresh_evidence(r, evidence.get(r.id, [])))
            or any(d.get("status") == "pass" for d in (r.validation or {}).get("deterministic", []))
        ]
        primary = _ai_validate(session, opportunity_id, candidates, evidence, settings=settings, provider_name=None, label="ai_validation", warnings=warnings)
        second_provider = settings.ai_secondary_review_provider or settings.ai_review_provider
        redundant = [
            r for r in candidates
            if r.severity == "critical"
            or (
                settings.compliance_high_confidence_threshold is not None
                and (primary.get(r.id) or {}).get("confidence") is not None
                and primary[r.id]["confidence"] < settings.compliance_high_confidence_threshold
            )
        ]
        if redundant and second_provider:
            warnings += independence_warnings(
                settings, second_provider, None, code="secondary_validator_not_independent", what="The secondary validator"
            )
            secondary = _ai_validate(session, opportunity_id, redundant, evidence, settings=settings, provider_name=second_provider, label="ai_validation_secondary", warnings=warnings)
        elif redundant:
            warnings.append({
                "code": "secondary_validator_unconfigured",
                "severity": "medium",
                "message": f"{len(redundant)} requirement(s) need redundant validation but no AI_SECONDARY_REVIEW_PROVIDER/AI_REVIEW_PROVIDER is set; they need a second method (deterministic, proposal scan, or human).",
            })
    else:
        primary = {r.id: (r.validation or {})["ai"] for r in requirements if (r.validation or {}).get("ai")}
        secondary = {r.id: (r.validation or {})["ai_secondary"] for r in requirements if (r.validation or {}).get("ai_secondary")}

    run = record_run(session, opportunity_id=opportunity_id, run_type="compliance_validation", run_version=VALIDATION_VERSION, output={}, warnings=warnings)
    decisions: dict[str, Any] = {}
    changed = 0
    for req in requirements:
        validation = req.validation or {}
        decision = decide_status(
            inputs_for(
                req,
                evidence.get(req.id, []),
                ai_primary=primary.get(req.id),
                ai_secondary=secondary.get(req.id),
                settings=settings,
                jev_human_interpretation=bool(validation.get("jev_human_interpretation")),
            )
        )
        if primary.get(req.id) or secondary.get(req.id):
            req.validation = {**(req.validation or {}), "ai": primary.get(req.id), "ai_secondary": secondary.get(req.id)}
        changed += int(apply_decision(req, decision, run_id=run.id))
        decisions[str(req.id)] = {"status": decision.status, "reason": decision.reason, "methods": decision.methods, "blocks_submission": req.blocks_submission}
        for claim in decision.blocked_ai_claims:
            upsert_open_finding(
                session,
                opportunity_id=opportunity_id,
                requirement_id=req.id,
                finding_type="ai_claim_blocked",
                severity="high" if req.severity in {"critical", "high"} else "medium",
                description=claim,
                detected_by="status_gate",
                detector_version=VALIDATION_VERSION,
                source_refs={"ai": primary.get(req.id), "ai_secondary": secondary.get(req.id), "deterministic": validation.get("deterministic")},
                blocks_submission=False,
                compliance_run_id=run.id,
            )
    run.output_json = {"decisions": decisions, "changed": changed, "ai_primary": len(primary), "ai_secondary": len(secondary)}
    session.flush()
    return {"run_id": run.id, "changed": changed, "warnings": warnings, "decisions": decisions}


def compliance_state(requirements: list[Requirement], counts: dict[str, Any]) -> dict[str, Any]:
    return {
        "mandatory_total": counts["mandatory_total"],
        "mandatory_missing": counts["mandatory_total"] - counts["mandatory_satisfied"],
        "needs_review": counts["mandatory_needs_review"] + counts["mandatory_unknown"],
        "critical_total": counts["critical_total"],
        "critical_unresolved": counts["critical_unresolved"],
        "country_of_origin_conflict": any(
            r.requirement_type == "country_of_origin" and r.status in {"missing"} for r in requirements
        ),
        "requirements": [
            {
                "requirement_id": r.id,
                "mandatory": r.mandatory,
                "severity": r.severity,
                "status": r.status,
                "independently_confirmed": r.independently_confirmed,
                "deterministic_fail": bool((r.validation or {}).get("deterministic_fail")),
                "blocks_submission": r.blocks_submission,
            }
            for r in requirements
        ],
    }


def run_jev_routing(
    session: Session,
    opportunity_id: int,
    *,
    amendment: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Route matrix risk through the Phase 8 ``compliance_and_amendment`` bundle."""
    from govcon.compliance.metrics import coverage_counts
    from govcon.decision.engine import build_decision_state, run_decision_bundle

    settings = settings or get_settings()
    requirements = active_requirements(session, opportunity_id)
    prior_blocks = {req.id: req.blocks_submission for req in requirements}
    for req in requirements:
        validation = dict(req.validation or {})
        if validation.pop("jev_blocks_submission", False):
            req.blocks_submission = req.status not in RESOLVED_STATUSES and (req.mandatory is not False or req.severity == "critical")
        validation.pop("jev_human_interpretation", None)
        req.validation = validation
    counts = coverage_counts(requirements, [])
    state = build_decision_state(session, opportunity_id)
    state["compliance"] = compliance_state(requirements, counts)
    if amendment is not None:
        state["amendment"] = {"count": amendment.get("amendment_count", 0), "material": bool(amendment.get("material"))}
    execution = run_decision_bundle(session, opportunity_id=opportunity_id, bundle_name="compliance_and_amendment", state=state, settings=settings)
    by_id = {r.id: r for r in requirements}
    added_blocks: list[int] = []
    routed_review: list[int] = []
    for item in execution.result.get("requirement_decisions", []):
        rid = item.get("requirement_id")
        decided_requirement = by_id.get(rid) if isinstance(rid, int) else None
        if decided_requirement is None:
            continue
        validation = dict(decided_requirement.validation or {})
        if item.get("blocks_submission") and decided_requirement.status not in RESOLVED_STATUSES:
            validation["jev_blocks_submission"] = True
            decided_requirement.blocks_submission = True
            added_blocks.append(decided_requirement.id)
        if item.get("human_interpretation_required"):
            validation["jev_human_interpretation"] = True
            if decided_requirement.status == "satisfied" and not validation.get("override"):
                decided_requirement.status = "needs_review"
                decided_requirement.status_reason = "decision layer requires human interpretation"
                decided_requirement.version = (decided_requirement.version or 1) + 1
                routed_review.append(decided_requirement.id)
        decided_requirement.validation = validation
    for req in requirements:
        if req.blocks_submission != prior_blocks[req.id]:
            req.version = (req.version or 1) + 1
    output = {
        "decision_run_id": execution.run.id,
        "provider": execution.provider,
        "model": execution.model,
        "result": execution.result,
        "added_blocks": added_blocks,
        "routed_to_review": routed_review,
        "second_validation_required": execution.result.get("second_validation_required"),
        "note": "JEV blockers are recomputed on each routing run; matrix blockers remain in force.",
    }
    run = record_run(session, opportunity_id=opportunity_id, run_type="jev_routing", run_version=ROUTING_VERSION, output=output, source_snapshot_ids=state.get("source_snapshot_ids"))
    return {"run_id": run.id, **output}
