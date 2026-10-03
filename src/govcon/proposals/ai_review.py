"""Proposal red-team AI review (Phase 11 — §41.2).

Runs the ``proposal_red_team`` prompt against a selected ``ProposalVersion``.
The red-team model is DeepSeek by default; JEV routes severity-classified
issues and determines whether an additional model or human review is needed.

Critical findings are recorded as blocking ``ComplianceFinding`` rows. The
result is persisted to ``ai_analyses`` and linked back to the ``Proposal``
record.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from govcon.ai.analysis_types import AnalysisType
from govcon.ai.structured import run_structured_prompt
from govcon.compliance.matrix import (
    active_requirements,
    upsert_open_finding,
)
from govcon.config import Settings, get_settings
from govcon.models import (
    Proposal,
    ProposalVersion,
)
from govcon.proposals.versions import get_sections_for_version
from govcon.security.classification import DataClassification
from govcon.workflow.transitions import can_transition

logger = logging.getLogger("govcon.proposals.ai_review")

RED_TEAM_PROMPT = "proposal_red_team"


def _build_requirements_json(requirements) -> str:
    rows = [
        {
            "id": r.id,
            "requirement_type": r.requirement_type,
            "requirement_text": r.requirement_text,
            "mandatory": r.mandatory,
            "severity": r.severity,
            "response_required": r.response_required,
            "status": r.status,
            "blocks_submission": r.blocks_submission,
            "stale_due_to_amendment": r.stale_due_to_amendment,
        }
        for r in requirements
    ]
    return json.dumps(rows, default=str)


def _build_proposal_text(sections) -> str:
    parts: list[str] = []
    for s in sections:
        heading = s.heading or s.section_key or "Section"
        parts.append(f"## [{s.section_key}] {heading}\n\n{s.content or ''}")
    return "\n\n".join(parts)


def run_proposal_red_team(
    session: Session,
    *,
    opportunity_id: int,
    proposal_version_id: int,
    company_facts: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Run the red-team prompt against a proposal version.

    Critical findings from the red-team become blocking ``ComplianceFinding``
    rows. The AI analysis row is returned and linked to ``Proposal.red_team_analysis_id``.

    Returns a dict with keys:
      - ``analysis_id``: the ``AIAnalysis.id``
      - ``result``: parsed ``ProposalRedTeamV1`` dict
      - ``critical_count``, ``major_count``, ``minor_count``
      - ``overall_assessment``: "READY" | "NOT_READY" | "NEEDS_REVIEW"
    """
    settings = settings or get_settings()

    pv = session.get(ProposalVersion, proposal_version_id)
    if pv is None:
        raise ValueError(f"ProposalVersion {proposal_version_id} not found")

    proposal = session.get(Proposal, pv.proposal_id)
    if proposal is None or proposal.opportunity_id != opportunity_id:
        raise ValueError(f"ProposalVersion {proposal_version_id} does not belong to opportunity {opportunity_id}")

    requirements = active_requirements(session, opportunity_id=opportunity_id)
    sections = get_sections_for_version(session, proposal_version_id)

    company_facts_data = company_facts
    if company_facts_data is None:
        from govcon.compliance.pipeline import load_company_facts

        # The same facts compliance uses: SAM registration overlaid, stale values unknown.
        company_facts_data = load_company_facts(settings, session) or None

    variables = {
        "REQUIREMENTS_JSON": _build_requirements_json(requirements),
        "PROPOSAL_TEXT": _build_proposal_text(sections),
        "APPROVED_FACTS_JSON": json.dumps(company_facts_data or {"status": "UNKNOWN"}, default=str),
    }

    context_manifest = {
        "opportunity_id": opportunity_id,
        "proposal_version_id": proposal_version_id,
        "section_count": len(sections),
        "requirement_count": len(requirements),
    }

    result = run_structured_prompt(
        session,
        classification=DataClassification.PROPRIETARY,
        opportunity_id=opportunity_id,
        prompt_name=RED_TEAM_PROMPT,
        analysis_type=AnalysisType.PROPOSAL_RED_TEAM,
        variables=variables,
        context_manifest=context_manifest,
        settings=settings,
    )

    output = result.output.model_dump()
    analysis = result.analysis

    # Record critical findings as blocking compliance findings
    # Build a set of valid requirement IDs to avoid FK violations
    valid_req_ids = {r.id for r in requirements}

    for finding in output.get("findings", []):
        if finding.get("severity") == "critical":
            req_id = finding.get("requirement_id")
            # Only reference requirement_id when it exists in this opportunity
            if req_id is not None and req_id not in valid_req_ids:
                req_id = None
            upsert_open_finding(
                session,
                opportunity_id=opportunity_id,
                finding_type="red_team_critical",
                severity="critical",
                description=finding.get("description", "Red-team critical finding"),
                source_refs={"red_team_finding": finding},
                blocks_submission=True,
                detected_by="proposal_red_team.v1",
                detector_version="1",
                requirement_id=req_id,
            )

    # Link analysis to proposal
    if analysis is not None:
        proposal.red_team_analysis_id = analysis.id
        # Only a red team of the current version moves the proposal to red_teamed.
        if proposal.current_version_id == proposal_version_id and can_transition(
            "proposal", proposal.status, "red_teamed"
        ) and proposal.status in {"draft", "ai_generated", "returned_for_fix"}:
            proposal.status = "red_teamed"
            proposal.version = (proposal.version or 1) + 1
        session.flush()

    return {
        "analysis_id": analysis.id if analysis else None,
        "result": output,
        "critical_count": output.get("critical_count", 0),
        "major_count": output.get("major_count", 0),
        "minor_count": output.get("minor_count", 0),
        "overall_assessment": output.get("overall_assessment", "NEEDS_REVIEW"),
    }
