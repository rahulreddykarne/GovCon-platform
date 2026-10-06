"""Resolved product facts without overwriting government-feed records."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.ai.analysis_types import AnalysisType
from govcon.ai.schemas import SolicitationAnalysisV1
from govcon.matching.pricing import canonical_nsn
from govcon.models import AIAnalysis, Opportunity
from govcon.workflow.source_revision import current_source_revision, is_stale, stamp_of


@dataclass(frozen=True)
class ProductFacts:
    nsn: str | None
    quantity: Decimal | None
    unit: str | None
    source_analysis_id: int | None = None


def _quantity(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() and number >= 0 else None


def product_facts_from_summary(opportunity: Opportunity, analysis: AIAnalysis | None) -> ProductFacts:
    """Resolve a summary the caller has already checked against its source revision.

    Feed facts win. Duplicate batch fragments are compared, never summed.
    Multiple or unidentified products cannot supply a single quote quantity.
    """
    source = ProductFacts(
        canonical_nsn(opportunity.nsn) or opportunity.nsn,
        _quantity(opportunity.quantity),
        (opportunity.unit or "").strip().upper() or None,
    )
    if source.nsn and source.quantity is not None and source.unit:
        return source
    if analysis is None or analysis.schema_version != "solicitation_analysis.v1":
        return source
    try:
        summary = SolicitationAnalysisV1.model_validate(analysis.output_json)
    except ValueError:
        return source
    items = summary.items
    nsns = {nsn for item in items if (nsn := canonical_nsn(item.nsn))}
    if len(nsns) > 1 or (source.nsn and nsns and source.nsn not in nsns):
        return source
    if nsns and any(canonical_nsn(item.nsn) is None for item in items):
        # A quantity on another, unidentified row is not evidence for this NSN.
        return source
    # Without a product identity, several distinct rows cannot be combined.
    if not nsns and len(items) != 1:
        return source
    quantities = {quantity for item in items if (quantity := _quantity(item.quantity)) is not None}
    units = {item.unit.strip().upper() for item in items if item.unit and item.unit.strip()}
    nsn = source.nsn or (next(iter(nsns)) if len(nsns) == 1 else None)
    quantity = source.quantity if source.quantity is not None else (next(iter(quantities)) if len(quantities) == 1 else None)
    unit = source.unit or (next(iter(units)) if len(units) == 1 else None)
    derived = (nsn, quantity, unit) != (source.nsn, source.quantity, source.unit)
    return ProductFacts(nsn, quantity, unit, analysis.id if derived else None)


def effective_product_facts(session: Session, opportunity: Opportunity) -> ProductFacts:
    """Load a current summary for callers that have not already loaded one."""
    source = product_facts_from_summary(opportunity, None)
    if source.nsn and source.quantity is not None and source.unit:
        return source
    analysis = session.scalar(select(AIAnalysis).where(
        AIAnalysis.opportunity_id == opportunity.id,
        AIAnalysis.analysis_type == AnalysisType.SOLICITATION_SUMMARY,
        AIAnalysis.schema_version == "solicitation_analysis.v1",
    ).order_by(AIAnalysis.created_at.desc(), AIAnalysis.id.desc()).limit(1))
    if analysis is None or is_stale(stamp_of(analysis.context_manifest), current_source_revision(session, opportunity.id)):
        return source
    return product_facts_from_summary(opportunity, analysis)
