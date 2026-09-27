"""Outcome capture service (Phase 15).

Records terminal outcomes (won / lost / no_bid / cancelled) with structured
fields per §21. Auto-denormalizes opportunity fields for analytics.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.models import AIAnalysis, Opportunity, OutcomeFeedback, Pursuit

# Allowed structured no-bid categories (matches Phase 15 spec §21)
NO_BID_CATEGORIES = frozenset(
    {
        "missing_capability",
        "margin",
        "deadline",
        "supplier_availability",
        "eligibility",
        "compliance_issue",
        "competition",
        "strategic_choice",
        "other",
    }
)

# Allowed outcome values
TERMINAL_OUTCOMES = frozenset({"won", "lost", "no_bid", "cancelled"})


def _denorm_from_opportunity(opp: Opportunity) -> dict[str, Any]:
    """Extract denormalized analytics fields from an opportunity."""
    # Use agency_path (may be a full path like "DEPT OF DEFENSE > DLA > ...")
    agency = opp.agency_path
    # Use the midpoint of the estimated value range, or min if max is None
    est_value = None
    if opp.estimated_value_min is not None and opp.estimated_value_max is not None:
        est_value = (opp.estimated_value_min + opp.estimated_value_max) / 2
    elif opp.estimated_value_min is not None:
        est_value = opp.estimated_value_min
    elif opp.estimated_value_max is not None:
        est_value = opp.estimated_value_max
    return {
        "denorm_agency": agency,
        "denorm_psc": opp.psc_code,
        "denorm_naics": opp.naics_code,
        "denorm_estimated_value": est_value,
    }


def record_outcome(
    session: Session,
    *,
    opportunity_id: int,
    outcome: str,
    # No-bid
    no_bid_reason: str | None = None,
    no_bid_category: str | None = None,
    # Loss
    loss_reason: str | None = None,
    known_winning_price: float | None = None,
    # Win
    win_reason: str | None = None,
    win_margin_pct: float | None = None,
    win_supplier: str | None = None,
    win_delivery_terms: str | None = None,
    win_proposal_version: str | None = None,
    # Common
    awarded_vendor_uei: str | None = None,
    awarded_vendor_name: str | None = None,
    award_amount: float | None = None,
    award_date: str | None = None,
    government_feedback: str | None = None,
    debrief_notes: str | None = None,
    lessons_learned: str | None = None,
) -> OutcomeFeedback:
    """Record a terminal outcome with structured capture fields.

    Automatically denormalizes agency/PSC/NAICS/value from the opportunity
    for analytics use.
    """
    if outcome not in TERMINAL_OUTCOMES:
        raise ValueError(f"outcome must be one of {sorted(TERMINAL_OUTCOMES)}")
    if no_bid_category is not None and no_bid_category not in NO_BID_CATEGORIES:
        raise ValueError(
            f"no_bid_category must be one of {sorted(NO_BID_CATEGORIES)} or None"
        )

    opp = session.get(Opportunity, opportunity_id)
    if opp is None:
        raise ValueError(f"opportunity {opportunity_id} not found")

    pursuit = session.scalar(
        select(Pursuit).where(Pursuit.opportunity_id == opportunity_id)
    )
    denorm = _denorm_from_opportunity(opp)

    now = datetime.now(UTC)
    row = OutcomeFeedback(
        opportunity_id=opportunity_id,
        pursuit_id=pursuit.id if pursuit else None,
        outcome=outcome,
        no_bid_reason=no_bid_reason,
        no_bid_category=no_bid_category,
        loss_reason=loss_reason,
        known_winning_price=Decimal(str(known_winning_price)) if known_winning_price is not None else None,
        win_reason=win_reason,
        win_margin_pct=Decimal(str(win_margin_pct)) if win_margin_pct is not None else None,
        win_supplier=win_supplier,
        win_delivery_terms=win_delivery_terms,
        win_proposal_version=win_proposal_version,
        awarded_vendor_uei=awarded_vendor_uei,
        awarded_vendor_name=awarded_vendor_name,
        award_amount=Decimal(str(award_amount)) if award_amount is not None else None,
        government_feedback=government_feedback,
        debrief_notes=debrief_notes,
        lessons_learned=lessons_learned,
        **denorm,
    )
    session.add(row)

    # Sync pursuit stage
    if pursuit is not None:
        stage_map = {"won": "won", "lost": "lost", "no_bid": "no_bid", "cancelled": "cancelled"}
        pursuit.stage = stage_map[outcome]
        pursuit.outcome_at = now
        pursuit.outcome_notes = (
            lessons_learned or win_reason or loss_reason or no_bid_reason
        )

    session.flush()
    return row


def outcome_to_dict(row: OutcomeFeedback) -> dict[str, Any]:
    """Serialise an OutcomeFeedback row to a plain dict for MCP/API output."""
    return {
        "id": row.id,
        "opportunity_id": row.opportunity_id,
        "pursuit_id": row.pursuit_id,
        "outcome": row.outcome,
        "no_bid_reason": row.no_bid_reason,
        "no_bid_category": row.no_bid_category,
        "loss_reason": row.loss_reason,
        "known_winning_price": float(row.known_winning_price) if row.known_winning_price is not None else None,
        "win_reason": row.win_reason,
        "win_margin_pct": float(row.win_margin_pct) if row.win_margin_pct is not None else None,
        "win_supplier": row.win_supplier,
        "win_delivery_terms": row.win_delivery_terms,
        "win_proposal_version": row.win_proposal_version,
        "awarded_vendor_uei": row.awarded_vendor_uei,
        "awarded_vendor_name": row.awarded_vendor_name,
        "award_amount": float(row.award_amount) if row.award_amount is not None else None,
        "award_date": row.award_date.isoformat() if row.award_date else None,
        "government_feedback": row.government_feedback,
        "debrief_notes": row.debrief_notes,
        "lessons_learned": row.lessons_learned,
        "denorm_agency": row.denorm_agency,
        "denorm_psc": row.denorm_psc,
        "denorm_naics": row.denorm_naics,
        "denorm_estimated_value": float(row.denorm_estimated_value) if row.denorm_estimated_value is not None else None,
        "outcome_analysis_id": row.outcome_analysis_id,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
