"""Producers for the market, supplier (sourcing), and pricing AI analyses.

Each builds deterministic, source-backed inputs, runs the registry prompt
through ``run_structured_prompt`` (classification-checked), and stores the
validated result as an ``ai_analyses`` row of the matching ``AnalysisType``
with the source revision it was built from. ``prepare_analysis`` builds the
same call for the durable ``ai_analysis`` task (ADR-064), whose provider call
runs with no transaction open.

- Market analysis uses public award and opportunity data → PUBLIC.
- Supplier and pricing analysis use the pursuit's supplier, cost, and price
  → PROPRIETARY (blocked unless AI_EXTERNAL_ALLOWED_FOR_PROPRIETARY=true).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.ai.analysis_types import AnalysisType
from govcon.ai.structured import (
    PreparedCall,
    prepare_structured_call,
    run_structured_prompt,
)
from govcon.compliance.matrix import active_requirements
from govcon.config import Settings, get_settings
from govcon.intelligence.competitors import competitor_summary
from govcon.matching.pricing import PricePoint, recent_award_comps
from govcon.models import AIAnalysis, Opportunity, Pursuit, Vendor
from govcon.security.classification import DataClassification
from govcon.workflow.source_revision import SOURCE_REVISION_KEY, current_source_revision

SOURCING_REQUIREMENT_TYPES = frozenset(
    {"technical", "delivery", "country_of_origin", "packaging", "quality", "certification", "item", "marking"}
)


class AnalysisInputMissing(ValueError):
    """The deterministic inputs an analysis needs are not recorded yet."""


def _num(value: Decimal | float | None) -> float | None:
    return float(value) if value is not None else None


def _opportunity(session: Session, opportunity_id: int) -> Opportunity:
    opportunity = session.get(Opportunity, opportunity_id)
    if opportunity is None:
        raise ValueError(f"opportunity not found: {opportunity_id}")
    return opportunity


def _opportunity_json(opp: Opportunity) -> dict[str, Any]:
    return {
        "id": opp.id,
        "source": opp.source,
        "solicitation_number": opp.solicitation_number,
        "title": opp.title,
        "agency": opp.agency_path,
        "psc_code": opp.psc_code,
        "naics_code": opp.naics_code,
        "set_aside_code": opp.set_aside_code,
        "nsn": opp.nsn,
        "quantity": _num(opp.quantity),
        "unit": opp.unit,
        "response_deadline": opp.response_deadline.isoformat() if opp.response_deadline else None,
    }


def _award_json(point: PricePoint) -> dict[str, Any]:
    return {
        "award_id": point.award_id,
        "piid": point.piid,
        "vendor_name": point.vendor_name,
        "vendor_uei": point.vendor_uei,
        "action_date": point.action_date.isoformat() if point.action_date else None,
        "total_obligation": _num(point.amount),
        "unit_price": _num(point.unit_price),
        "quantity": _num(point.quantity),
        "nsn": point.nsn,
        "psc_code": point.psc_code,
        "awarding_agency": point.awarding_agency,
        "description": (point.description or "")[:500] or None,
    }


def _request(
    session: Session,
    opportunity: Opportunity,
    *,
    prompt_name: str,
    analysis_type: AnalysisType,
    variables: dict[str, Any],
    classification: DataClassification,
    settings: Settings,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Keyword arguments for the structured call, stamped with the source revision."""
    manifest = {**manifest, SOURCE_REVISION_KEY: current_source_revision(session, opportunity.id)}
    return {
        "opportunity_id": opportunity.id,
        "prompt_name": prompt_name,
        "analysis_type": analysis_type,
        "variables": variables,
        "context_manifest": manifest,
        "settings": settings,
        "classification": classification,
    }


def _market_request(session: Session, opportunity_id: int, settings: Settings) -> dict[str, Any]:
    opp = _opportunity(session, opportunity_id)
    comps = recent_award_comps(session, nsn=opp.nsn, psc_code=opp.psc_code, limit=25)
    summary = competitor_summary(session, opp.id, limit=10)
    profiles: list[dict[str, Any]] = []
    seen: set[str] = set()
    for bucket in summary.buckets if summary else ():
        for winner in bucket.winners:
            key = winner.recipient_uei or winner.recipient_name or ""
            if not key or key in seen:
                continue
            seen.add(key)
            vendor = session.get(Vendor, winner.recipient_uei) if winner.recipient_uei else None
            profiles.append(
                {
                    "uei": winner.recipient_uei,
                    "name": winner.recipient_name,
                    "award_count": winner.award_count,
                    "total_obligation": _num(winner.total_obligation),
                    "matched_on": bucket.dimension,
                    "registration_status": vendor.registration_status if vendor else None,
                    "business_types": vendor.business_types if vendor else None,
                }
            )
    return _request(
        session,
        opp,
        prompt_name="market_analysis",
        analysis_type=AnalysisType.MARKET,
        variables={
            "OPPORTUNITY_JSON": _opportunity_json(opp),
            "AWARDS_JSON": [_award_json(p) for p in comps],
            "VENDOR_PROFILES_JSON": profiles,
        },
        classification=DataClassification.PUBLIC,
        settings=settings,
        manifest={"award_ids": [p.award_id for p in comps], "vendor_count": len(profiles)},
    )


def _supplier_request(session: Session, opportunity_id: int, settings: Settings) -> dict[str, Any]:
    from govcon.sourcing.records import quote_records

    opp = _opportunity(session, opportunity_id)
    pursuit = session.scalar(select(Pursuit).where(Pursuit.opportunity_id == opp.id))
    # Current supplier quotes (ADR-071) are the primary evidence; the pursuit's
    # free-text supplier and cost remain accepted for older pursuits.
    records = quote_records(session, opp.id)
    if pursuit is not None and (pursuit.supplier or pursuit.sourcing_cost is not None):
        records.append({
            "supplier": pursuit.supplier,
            "sourcing_cost": _num(pursuit.sourcing_cost),
            "notes": pursuit.notes,
            "source": "pursuit record entered by a user",
        })
    if not records:
        raise AnalysisInputMissing(
            "record a current supplier quote, or a supplier or sourcing cost on the pursuit, before running "
            "supplier analysis"
        )
    requirements = [
        r for r in active_requirements(session, opp.id) if (r.requirement_type or "") in SOURCING_REQUIREMENT_TYPES
    ]
    return _request(
        session,
        opp,
        prompt_name="supplier_analysis",
        analysis_type=AnalysisType.SOURCING,
        variables={
            "REQUIREMENTS_JSON": [
                {
                    "requirement_id": r.id,
                    "text": r.requirement_text,
                    "type": r.requirement_type,
                    "mandatory": r.mandatory,
                    "status": r.status,
                    "key_values": r.key_values,
                }
                for r in requirements
            ],
            "SUPPLIER_RECORDS_JSON": records,
        },
        classification=DataClassification.PROPRIETARY,
        settings=settings,
        manifest={"requirement_ids": [r.id for r in requirements], "pursuit_id": pursuit.id if pursuit else None,
                  "quote_ids": [r["quote_id"] for r in records if "quote_id" in r]},
    )


def _pricing_request(session: Session, opportunity_id: int, settings: Settings) -> dict[str, Any]:
    opp = _opportunity(session, opportunity_id)
    from govcon.sourcing.records import lowest_current_total

    pursuit = session.scalar(select(Pursuit).where(Pursuit.opportunity_id == opp.id))
    quote_price = pursuit.quote_price if pursuit is not None else None
    cost = pursuit.sourcing_cost if pursuit is not None else None
    cost_source = "pursuit record entered by a user" if cost is not None else None
    best = lowest_current_total(session, opp.id) if cost is None else None
    if best is not None:
        # The lowest current supplier quote stands in for an unrecorded cost (ADR-071).
        cost, cost_source = best.total_price, f"lowest current supplier quote #{best.id}"
    if quote_price is None and cost is None:
        raise AnalysisInputMissing(
            "record a quote price or sourcing cost on the pursuit, or a current supplier quote, before running "
            "pricing analysis"
        )
    comps = recent_award_comps(session, nsn=opp.nsn, psc_code=opp.psc_code, limit=25)
    from govcon.sourcing.product_facts import effective_product_facts

    facts = effective_product_facts(session, opp)
    quantity = facts.quantity if facts.quantity is not None and facts.quantity > 0 else None
    markup = ((quote_price - cost) / cost * 100) if quote_price is not None and cost else None
    inputs = {
        "quote_price_total": _num(quote_price),
        "sourcing_cost_total": _num(cost),
        "sourcing_cost_source": cost_source,
        "markup_on_cost_pct": _num(markup),
        "quantity": _num(quantity),
        "unit": facts.unit,
        "product_facts_analysis_id": facts.source_analysis_id,
        "proposed_unit_price": _num(quote_price / quantity) if quote_price is not None and quantity else None,
        "unit_cost": _num(cost / quantity) if cost is not None and quantity else None,
        "note": "Arithmetic is computed by the application; markup_on_cost_pct = (price - cost) / cost.",
    }
    return _request(
        session,
        opp,
        prompt_name="pricing_analysis",
        analysis_type=AnalysisType.PRICING,
        variables={
            "PRICING_INPUTS_JSON": inputs,
            "HISTORICAL_AWARDS_JSON": [_award_json(p) for p in comps],
        },
        classification=DataClassification.PROPRIETARY,
        settings=settings,
        manifest={"award_ids": [p.award_id for p in comps], "pursuit_id": pursuit.id if pursuit else None,
                  "cost_source": cost_source},
    )


_REQUESTS = {
    "market": _market_request,
    "supplier": _supplier_request,
    "pricing": _pricing_request,
}
ANALYSIS_KINDS = tuple(_REQUESTS)


def prepare_analysis(session: Session, opportunity_id: int, kind: str, *, settings: Settings | None = None) -> PreparedCall:
    """Build the inputs and check policy in a transaction; the AI call happens later.

    Raises ``AnalysisInputMissing`` when the pursuit lacks the facts the
    analysis needs, and ``StructuredCallError`` when policy refuses the call.
    """
    settings = settings or get_settings()
    return prepare_structured_call(session, **_REQUESTS[kind](session, opportunity_id, settings))


def _run_kind(session: Session, opportunity_id: int, kind: str, settings: Settings | None) -> AIAnalysis:
    settings = settings or get_settings()
    result = run_structured_prompt(session, **_REQUESTS[kind](session, opportunity_id, settings))
    assert result.analysis is not None
    return result.analysis


def run_market_analysis(session: Session, opportunity_id: int, *, settings: Settings | None = None) -> AIAnalysis:
    return _run_kind(session, opportunity_id, "market", settings)


def run_supplier_analysis(session: Session, opportunity_id: int, *, settings: Settings | None = None) -> AIAnalysis:
    return _run_kind(session, opportunity_id, "supplier", settings)


def run_pricing_analysis(session: Session, opportunity_id: int, *, settings: Settings | None = None) -> AIAnalysis:
    return _run_kind(session, opportunity_id, "pricing", settings)


PRODUCERS = {
    "market": run_market_analysis,
    "supplier": run_supplier_analysis,
    "pricing": run_pricing_analysis,
}
