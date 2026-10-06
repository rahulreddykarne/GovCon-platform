"""AI proposal drafting engine (Phase 11 — §41.1).

Generates proposal sections from the verified compliance matrix using the
``proposal_drafting`` prompt. Only source-backed facts are used; unknown
fields become explicit ``[[BLOCKER:...]]`` placeholders. The writer model's
own claim that a requirement is covered is not sufficient — coverage is
verified separately by ``compliance.proposal_coverage``.

No-fabrication and evidence rules from the shared prompt policy are enforced
via the prompt; this module never invents facts.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.ai.analysis_types import AnalysisType
from govcon.ai.structured import (
    PreparedCall,
    StructuredCallResult,
    prepare_structured_call,
    run_structured_prompt,
)
from govcon.compliance.matrix import active_requirements
from govcon.config import Settings, get_settings
from govcon.models import (
    Opportunity,
    Pursuit,
    Requirement,
)
from govcon.security.classification import DataClassification

logger = logging.getLogger("govcon.proposals.drafting")

DRAFTING_PROMPT = "proposal_drafting"
STANDARD_SECTIONS = [
    "cover_letter",
    "executive_summary",
    "technical_response",
    "delivery_plan",
    "past_performance",
    "management_quality",
    "pricing_narrative",
    "representations_certifications",
    "required_forms",
    "attachments",
]

SECTION_HEADINGS: dict[str, str] = {
    "cover_letter": "Cover / Quote Letter",
    "executive_summary": "Executive Summary",
    "technical_response": "Technical / Product Response",
    "delivery_plan": "Delivery Plan",
    "past_performance": "Past Performance",
    "management_quality": "Management / Quality",
    "pricing_narrative": "Pricing Narrative",
    "representations_certifications": "Representations / Certifications",
    "required_forms": "Required Forms",
    "attachments": "Attachments",
}


def _build_requirements_json(requirements: list[Requirement]) -> str:
    """Serialize requirements for the AI context."""
    rows = []
    for r in requirements:
        rows.append({
            "id": r.id,
            "requirement_type": r.requirement_type,
            "requirement_text": r.requirement_text,
            "mandatory": r.mandatory,
            "severity": r.severity,
            "response_required": r.response_required,
            "status": r.status,
            "assigned_proposal_section": r.assigned_proposal_section,
            "source_section": r.source_section,
            "blocks_submission": r.blocks_submission,
            "stale_due_to_amendment": r.stale_due_to_amendment,
            "key_values": r.key_values,
        })
    return json.dumps(rows, default=str)


def _build_approved_facts_json(
    opportunity: Opportunity,
    pursuit: Pursuit | None,
    company_facts: dict[str, Any] | None,
) -> str:
    """Serialize approved facts available for drafting."""
    facts: dict[str, Any] = {}

    # Opportunity basics (public solicitation data — not fabricated)
    facts["solicitation_number"] = opportunity.solicitation_number
    facts["title"] = opportunity.title
    facts["agency_path"] = opportunity.agency_path
    facts["nsn"] = opportunity.nsn
    facts["quantity"] = str(opportunity.quantity) if opportunity.quantity else None
    facts["unit"] = opportunity.unit

    # Company facts (from approved company_facts.json if configured)
    if company_facts:
        facts["company"] = company_facts
    else:
        facts["company"] = {"status": "UNKNOWN — no approved company facts on file"}

    # Pursuit pricing if approved
    if pursuit:
        facts["pursuit_stage"] = pursuit.stage
        if pursuit.quote_price is not None:
            facts["quote_price"] = str(pursuit.quote_price)
        if pursuit.sourcing_cost is not None:
            facts["sourcing_cost"] = str(pursuit.sourcing_cost)
        if pursuit.supplier:
            facts["supplier"] = pursuit.supplier

    return json.dumps(facts, default=str)


def _sections_for_opportunity(requirements: list[Requirement]) -> list[str]:
    """Determine which sections to draft based on requirement types present."""
    types_present: set[str | None] = {r.requirement_type for r in requirements}
    sections: list[str] = ["cover_letter", "executive_summary"]

    if any(t in types_present for t in {"technical", "country_of_origin", "delivery"}):
        sections.append("technical_response")
    if "delivery" in types_present:
        sections.append("delivery_plan")
    if "past_performance" in types_present:
        sections.append("past_performance")
    if "pricing" in types_present:
        sections.append("pricing_narrative")
    if "certification" in types_present or "representation" in types_present:
        sections.append("representations_certifications")
    if "set_aside" in types_present or "administrative" in types_present:
        sections.append("required_forms")

    return sections


def draft_proposal(
    session: Session,
    *,
    opportunity_id: int,
    sections_requested: list[str] | None = None,
    company_facts: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Run the proposal_drafting AI prompt and return the parsed output.

    When ``sections_requested`` is None, sections are inferred from requirement
    types present in the compliance matrix.

    Returns the validated ``ProposalDraftV1`` dict. Raises ``StructuredCallError``
    when the AI is unavailable or returns malformed output (fails closed).
    """
    request = _draft_request(
        session, opportunity_id=opportunity_id, sections_requested=sections_requested,
        company_facts=company_facts, settings=settings,
    )
    return draft_output(run_structured_prompt(session, **request))


def prepare_draft_call(
    session: Session,
    *,
    opportunity_id: int,
    sections_requested: list[str] | None = None,
    company_facts: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> PreparedCall:
    """Resolve the drafting call in a transaction; workers execute it later without one."""
    request = _draft_request(
        session, opportunity_id=opportunity_id, sections_requested=sections_requested,
        company_facts=company_facts, settings=settings,
    )
    return prepare_structured_call(session, **request)


def _draft_request(
    session: Session,
    *,
    opportunity_id: int,
    sections_requested: list[str] | None,
    company_facts: dict[str, Any] | None,
    settings: Settings | None,
) -> dict[str, Any]:
    """Keyword arguments for the drafting structured call."""
    settings = settings or get_settings()

    opp = session.get(Opportunity, opportunity_id)
    if opp is None:
        raise ValueError(f"Opportunity {opportunity_id} not found")

    requirements = active_requirements(session, opportunity_id=opportunity_id)
    if not requirements:
        logger.warning("opportunity %d has no active requirements; draft will be empty", opportunity_id)

    pursuit = session.scalars(
        select(Pursuit).where(Pursuit.opportunity_id == opportunity_id)
    ).first()

    sections = sections_requested or _sections_for_opportunity(requirements)
    sections_label = ", ".join(sections)

    company_facts_data = company_facts
    if company_facts_data is None:
        from govcon.compliance.pipeline import load_company_facts

        # The same facts compliance uses: SAM registration overlaid, stale values unknown.
        company_facts_data = load_company_facts(settings, session) or None

    variables = {
        "REQUIREMENTS_JSON": _build_requirements_json(requirements),
        "SECTIONS_REQUESTED": sections_label,
        "APPROVED_FACTS_JSON": _build_approved_facts_json(opp, pursuit, company_facts_data),
    }

    context_manifest = {
        "opportunity_id": opportunity_id,
        "sections_requested": sections,
        "requirement_count": len(requirements),
    }

    return {
        "classification": DataClassification.PROPRIETARY,
        "opportunity_id": opportunity_id,
        "prompt_name": DRAFTING_PROMPT,
        "analysis_type": AnalysisType.PROPOSAL_DRAFTING,
        "variables": variables,
        "context_manifest": context_manifest,
        "settings": settings,
    }


def draft_output(result: StructuredCallResult) -> dict[str, Any]:
    """The draft dict ``generate_proposal`` consumes, from a persisted drafting call."""
    output = result.output.model_dump()
    output["provider"] = result.analysis.provider
    output["model"] = result.analysis.model
    return output
