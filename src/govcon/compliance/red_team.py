"""Compliance red team (§15.15).

Runs after the matrix is built: "assume this bid will be rejected as
non-responsive; find every source-backed reason why." Two layers, both
persisted to ``compliance_findings``:

- a deterministic checklist over the matrix and inventory (confirmed
  findings — unresolved critical items, stale items, unacknowledged
  amendments, uncited requirements, incomplete source ingestion, deadline /
  timezone gaps, open conflicts, single-pass critical requirements);
- the ``compliance_red_team`` prompt (never the extraction prompts), whose
  findings keep their confirmed/possible certainty. AI findings can add
  blockers; they cannot clear anything.
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
from govcon.compliance.matrix import (
    active_requirements,
    close_undetected_findings,
    evidence_for,
    open_findings,
    record_run,
    upsert_open_finding,
)
from govcon.compliance.records import RESOLVED_STATUSES, Inventory
from govcon.compliance.schemas import ComplianceRedTeamV1
from govcon.config import Settings, get_settings
from govcon.diagnostics import trace_phase
from govcon.models import Requirement
from govcon.security.classification import DataClassification

RED_TEAM_VERSION = "compliance_red_team.v1"


def rule_findings(requirements: list[Requirement], inventory: Inventory, known_amendments: list[str]) -> list[dict[str, Any]]:
    """Pure checklist over matrix state."""
    out: list[dict[str, Any]] = []

    def add(finding_type, severity, description, requirement_id=None, blocks=False, refs=None):
        out.append({"finding_type": finding_type, "severity": severity, "description": description, "requirement_id": requirement_id, "blocks_submission": blocks, "source_refs": refs or {}})

    for req in requirements:
        unresolved = req.status not in RESOLVED_STATUSES
        potentially_mandatory = req.mandatory is not False
        if req.severity == "critical" and unresolved:
            add("critical_requirement_unresolved", "critical", f"Critical requirement is {req.status}: {req.requirement_text[:200]}", req.id, True, {"status_reason": req.status_reason})
        elif potentially_mandatory and req.status == "missing":
            add("missing_mandatory_requirement", "high", f"Mandatory requirement is missing: {req.requirement_text[:200]}", req.id, True, {"status_reason": req.status_reason})
        if req.status == "stale":
            add("stale_requirement", "high", f"Requirement conclusion is stale after an amendment: {req.requirement_text[:200]}", req.id, True)
        if req.source_file_id is None or not req.source_quote:
            add("uncited_requirement", "medium", f"Requirement has no established source location: {req.requirement_text[:200]}", req.id, False)
        if req.severity == "critical" and not req.independently_confirmed:
            add("critical_not_independently_confirmed", "medium", f"Critical requirement found by only {req.extraction_pass or 'one pass'}: {req.requirement_text[:200]}", req.id, False)
        if req.requirement_type == "country_of_origin" and unresolved:
            add("country_of_origin_issue", "high", f"Country-of-origin requirement is not established: {req.requirement_text[:200]}", req.id, potentially_mandatory)
        det = (req.validation or {}).get("deterministic") or []
        if any(d["validator"] == "deadline_timezone" and d["status"] != "pass" for d in det):
            add("deadline_timezone_unverified", "high", f"Deadline/timezone not verified: {next(d['reason'] for d in det if d['validator'] == 'deadline_timezone')}", req.id, potentially_mandatory)

    ack_types = [r for r in requirements if r.requirement_type == "amendment_acknowledgment"]
    if known_amendments and not ack_types:
        add("missing_amendment_acknowledgment_requirement", "critical", f"Amendments {', '.join(known_amendments)} exist but no acknowledgment requirement is tracked.", None, True)
    for warning in inventory.warnings:
        if warning.blocking:
            add("source_ingestion_incomplete", "critical", f"Source ingestion incomplete: {warning.message}", None, True, {"code": warning.code, "file_ids": warning.file_ids})
    if not any(r.requirement_type == "submission" for r in requirements):
        add("no_submission_instructions_found", "high", "No submission-instruction requirement was extracted; confirm method, recipient, and deadline manually.", None, True)
    return out


@trace_phase("compliance.red_team.run_red_team")
def run_red_team(
    session: Session,
    opportunity_id: int,
    inventory: Inventory,
    *,
    use_ai: bool = True,
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    requirements = active_requirements(session, opportunity_id)
    known = [f"{d.amendment_number:04d}" for d in inventory.amendments() if d.amendment_number]
    run = record_run(session, opportunity_id=opportunity_id, run_type="red_team", run_version=RED_TEAM_VERSION, output={})
    persisted: list[int] = []
    for item in rule_findings(requirements, inventory, known):
        finding = upsert_open_finding(
            session,
            opportunity_id=opportunity_id,
            requirement_id=item["requirement_id"],
            finding_type=item["finding_type"],
            severity=item["severity"],
            description=item["description"],
            detected_by="red_team_rules",
            detector_version=RED_TEAM_VERSION,
            source_refs=item["source_refs"],
            blocks_submission=item["blocks_submission"],
            certainty="confirmed",
            compliance_run_id=run.id,
        )
        persisted.append(finding.id)
    closed = close_undetected_findings(session, opportunity_id, detected_by="red_team_rules", keep_ids=set(persisted), run_id=run.id)

    warnings: list[dict[str, Any]] = []
    ai_summary: dict[str, Any] = {"status": "not_run"}
    if use_ai and requirements:
        by_id = {r.id: r for r in requirements}
        evidence = evidence_for(session, list(by_id))
        try:
            result = run_structured_prompt(
                session,
                classification=DataClassification.PROPRIETARY,
                opportunity_id=opportunity_id,
                prompt_name="compliance_red_team",
                analysis_type=AnalysisType.RED_TEAM_REVIEW,
                variables={
                    "REQUIREMENTS_JSON": [
                        {"requirement_id": r.id, "text": r.requirement_text, "type": r.requirement_type, "mandatory": r.mandatory, "severity": r.severity, "status": r.status, "status_reason": r.status_reason, "source": {"file_id": r.source_file_id, "page": r.source_page, "quote": r.source_quote}, "deterministic": (r.validation or {}).get("deterministic")}
                        for r in requirements
                    ],
                    "EVIDENCE_JSON": {
                        "evidence": [{"evidence_id": e.id, "requirement_id": e.requirement_id, "type": e.evidence_type, "status": e.verification_status, "description": e.description} for rows in evidence.values() for e in rows],
                        "open_findings": [{"type": f.finding_type, "severity": f.severity, "description": f.description} for f in open_findings(session, opportunity_id)],
                    },
                    "DOCUMENT_INVENTORY_JSON": [d.manifest() for d in inventory.documents],
                },
                context_manifest={"requirement_ids": sorted(by_id), "files": [d.file_id for d in inventory.documents]},
                settings=settings,
            )
            version = f"{result.prompt.name}@{result.prompt.version}:{result.prompt.content_hash[:12]}"
            for ai_finding in checked_output(result.output, ComplianceRedTeamV1).findings:
                rid = ai_finding.requirement_id if ai_finding.requirement_id in by_id else None
                finding = upsert_open_finding(
                    session,
                    opportunity_id=opportunity_id,
                    requirement_id=rid,
                    finding_type=ai_finding.finding_type,
                    severity=ai_finding.severity,
                    description=ai_finding.description,
                    detected_by="compliance_red_team_ai",
                    detector_version=version,
                    source_refs={"evidence": [e.model_dump() for e in ai_finding.evidence], "missing_evidence": ai_finding.missing_evidence, "ai_analysis_id": result.analysis.id},
                    blocks_submission=ai_finding.certainty == "confirmed" and ai_finding.severity in {"critical", "high"},
                    certainty=ai_finding.certainty,
                    compliance_run_id=run.id,
                )
                persisted.append(finding.id)
            ai_summary = {"status": "complete", "ai_analysis_id": result.analysis.id, "findings": len(checked_output(result.output, ComplianceRedTeamV1).findings)}
        except StructuredCallError as exc:
            warnings.append({"code": f"red_team_ai_{exc.reason}", "severity": "medium", "message": exc.detail})
            ai_summary = {"status": "failed", "reason": exc.reason}
    run.output_json = {"finding_ids": sorted(set(persisted)), "closed_finding_ids": closed, "ai": ai_summary}
    run.warnings = warnings or None
    session.flush()
    return {"run_id": run.id, **run.output_json}
